#!/usr/bin/env python3
"""A branded chatbot that reaches Claude only through the AI Gateway.

The browser talks to this server; this server talks to the gateway. It holds no
Anthropic credential -- the gateway injects the real key from the LLM provider's
upstream auth, which is the property the PoC exists to show.

The relay exists because the browser cannot call the gateway itself: the data
plane sends no CORS headers, and its certificate is one a page on localhost
cannot wave through.

The client does not choose the model. Anthropic's Messages API requires a
`model` field, so one is sent -- but it is a default the gateway's routing
policies may override, not a selection. Which model actually answered is read
back off the response, from the `model` on the `message_start` event.

    python3 server.py                 # http://localhost:8600

Configuration comes from config.env (see config.env.example) plus the gitignored
../secrets/gateway-keys.env (the *_API_KEY vars — a real credential, kept out of
the tracked config file); real environment variables win over both.
"""

import argparse
import base64
import hashlib
import http.cookies
import io
import itertools
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# Shared with the provisioning: where the gateways are, and which cards they can run.
sys.path.insert(0, os.path.join(HERE, "..", "scripts", "python"))
from gateway_settings import missing_gateway_settings  # noqa: E402
from target import LLM_GATEWAY, MCP_GATEWAY, ROOT, read_env_file, setting  # noqa: E402

# The showcase registry. Deliberately NOT under static/ -- _serve_static would
# hand the whole file, gateway wiring included, to any GET.
SHOWCASES_FILE = os.path.join(HERE, "showcases.json")

# The LLM providers this app can relay to, loaded from providers.json at the
# repo root, not defined here -- scripts/python/provision_showcases.py needs
# the exact same records (plus its own provisioning-only fields) to deploy
# each provider on the gateway, and two copies drifting apart (e.g. a model
# id here disagreeing with what was actually deployed) was a real, repeated
# bug. Each showcase lane exists once per provider on the gateway; this dict
# lets one showcase id resolve to any of them:
#   - messagesSuffix: fixed by the gateway's OpenAPI spec, not an operator
#     choice -- a provider mounted elsewhere needs a full
#     SHOWCASE_<ID>_<PROVIDER>_URL, not a different suffix.
#   - wire: the request/response shape to speak. "anthropic" is Claude's
#     Messages API; "openai" is the OpenAI-compatible chat/completions shape
#     Mistral and most others use (see _build_upstream_openai /
#     _relay_stream_openai / _relay_whole_openai). A new wire format needs a
#     builder + relay pair (see WIRES below); everything else is just a new
#     providers.json entry.
#   - models.small/large: the model ids provision_showcases.py deployed that
#     provider's routing/failover policies with (fast default vs. "deep"
#     route). Exposed via /api/providers so showcases.json's steps can say
#     modelRole: "small"/"large" instead of a literal id -- works correctly
#     under any provider, including one added after the step was written.
def _load_providers():
    path = os.path.join(os.path.dirname(HERE), "providers.json")
    with open(path) as handle:
        providers = json.load(handle)
    # "enabled": false takes a provider out of every endpoint below (its
    # showcases 503 as "not configured") without deleting its config --
    # useful for one whose key isn't set up yet, or one being retired.
    return {p["id"]: p for p in providers if p.get("enabled", True)}


PROVIDERS = _load_providers()
DEFAULT_PROVIDER_ID = next(iter(PROVIDERS))  # first entry in providers.json
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def _build_upstream_anthropic(showcase, messages, prompt):
    upstream = {"model": showcase["model"], "max_tokens": showcase["maxTokens"],
                "stream": showcase["stream"], "messages": messages}
    if prompt:
        upstream["system"] = prompt
    headers = {
        "content-type": "application/json",
        # Asking for SSE while sending stream:false is contradictory, and on
        # a cached path it is the kind of mismatch that makes a policy
        # decide the response is not cacheable.
        "accept": "text/event-stream" if showcase["stream"] else "application/json",
        "anthropic-version": CONF["ANTHROPIC_VERSION"],
    }
    return showcase["url"], upstream, headers


def _build_upstream_openai(showcase, messages, prompt):
    # No separate "system" field in the OpenAI shape -- a system prompt is
    # just another message, prepended.
    wire_messages = ([{"role": "system", "content": prompt}] if prompt else []) + messages
    upstream = {"model": showcase["model"], "max_tokens": showcase["maxTokens"],
                "stream": showcase["stream"], "messages": wire_messages}
    if showcase["model"] is None:   # the gateway picks it from the selector's header
        del upstream["model"]
    if showcase["stream"]:
        # Otherwise usage only ever comes back on the whole-response path --
        # OpenAI-compatible APIs omit it from stream chunks unless asked.
        upstream["stream_options"] = {"include_usage": True}
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream" if showcase["stream"] else "application/json",
    }
    return showcase["url"], upstream, headers


def _build_upstream_gemini(showcase, messages, prompt):
    # The one wire where the model isn't a request field: this provider's
    # gateway route mirrors Google's native path convention (confirmed via
    # GET /llm-provider-templates/gemini: requestModel.location is
    # "pathParam", not "payload" like the other two), so showcase["url"]
    # (built from a fixed messagesSuffix) is wrong here -- the URL must be
    # built per request from showcase["baseUrl"] + the model + the operation
    # (streaming is a distinct operation, not a body flag).
    op = "streamGenerateContent?alt=sse" if showcase["stream"] else "generateContent"
    url = f"{showcase['baseUrl']}/v1beta/models/{showcase['model']}:{op}"
    # Gemini's roles are "user"/"model", not "user"/"assistant".
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in messages]
    # Gemini's own equivalent of max_tokens -- nested under generationConfig,
    # not a top-level field like the other two wires. Without it Gemini falls
    # back to its own per-model ceiling, which is higher than MAX_TOKENS and
    # unbounded by MAX_TOKENS_CEILING -- exactly the unbounded-generation risk
    # that ceiling exists to prevent.
    upstream = {"contents": contents,
                "generationConfig": {"maxOutputTokens": showcase["maxTokens"]}}
    if prompt:
        upstream["systemInstruction"] = {"parts": [{"text": prompt}]}
    headers = {"content-type": "application/json"}
    return url, upstream, headers


# A showcase id is a dict key and nothing else -- never joined into a URL, a
# path or a filename. This bounds what can reach a log line or an error body.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CONVERSATION_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

# A provider context path: one or more plain path segments. Rejects a scheme,
# a protocol-relative //host, query, fragment, userinfo and whitespace -- but
# NOT ".." as a segment (tracked in TECH_DEBT.md; low real risk here since
# this isn't attacker-facing in this local demo).
CONTEXT_RE = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")

# SHOWCASE_* keys are not in DEFAULTS -- the ids live in showcases.json, not
# here -- so load_config's env pass cannot see them. This is the allowlist for
# a bounded second pass, not a general environment passthrough.
SHOWCASE_ENV_RE = re.compile(
    r"^SHOWCASE_[A-Z0-9_]+_(URL|CONTEXT|MODEL|MAX_TOKENS|SYSTEM_PROMPT|STREAM|API_KEY)$")

# A registry file must not be able to authorise an unbounded generation
# against a shared cost budget.
MAX_TOKENS_CEILING = 8192

# --model is a deliberate global stomp: `--model not-a-real-model` has to keep
# producing the gateway's verbatim 404 on every card, per-showcase override or
# not.
MODEL_OVERRIDE = None

DEFAULTS = {
    # The operator this assistant belongs to. Deliberately a placeholder: set
    # BRAND_NAME once and it flows into the wordmark, the page title and the
    # system prompt. Nothing else hard-codes a brand.
    "BRAND_NAME": "Acme Mobile",
    # A markdown file of product facts appended to the system prompt. It is the
    # assistant's only source of plan, price and allowance detail, so it can
    # answer "what plans do you offer" concretely instead of deflecting.
    # Point this somewhere else, or blank it, to change or remove the catalogue.
    "PLANS_FILE": "plans.md",
    # A reasoning model can spend output tokens on internal reasoning before it
    # writes a word. A small budget can be consumed entirely by that, returning
    # stop_reason max_tokens and no text at all -- verified against this
    # gateway with 32. Keep this generous.
    "MAX_TOKENS": "2048",
    # {brand} is substituted at request time. Leave empty to send no system
    # prompt at all.
    "SYSTEM_PROMPT": (
        "You are the AI assistant for {brand}, a mobile network operator. "
        "Be warm, concise and practical. Topics you cover include SIM-only plans, "
        "data allowances, top-ups, roaming, international calling, and account help. "
        "If a question needs account-specific data you cannot see, say so and "
        "explain where the customer can find it in their account. "
        "Never name, recommend or compare real-world mobile operators, networks or "
        "consumer brands, and never cite an external website. Refer to the operator "
        "only as {brand}."
    ),
    # The port the app listens on, from demo.env like every other port.
    "AI_CHATBOT_PORT": "8600",
    "TLS_VERIFY": "false",
    # Marker names/strings that identify a deliberate policy refusal. Empty by
    # default: nobody knows what the unreleased guardrail policies set yet, and
    # a showcase's gateway.blockStatus is the primary signal regardless.
    "POLICY_BLOCK_HEADERS": "",
    "POLICY_BLOCK_MARKERS": "",
    "GATEWAY_API_KEY": "",
    "GATEWAY_API_KEY_HEADER": "x-api-key",
    "ANTHROPIC_VERSION": "2023-06-01",
}

# Guard rails on what the browser may post back. Generous for a real
# conversation, small enough that a runaway page cannot bill the gateway.
MAX_MESSAGES = 200
MAX_BODY_BYTES = 256 * 1024

# MCP cards: how many model -> tool -> model rounds one turn may take, and
# what the agent is told on top of the configured prompt.
MCP_MAX_ROUNDS = 5
MCP_PROMPT = ("You are talking to a signed-in customer. The only actions you can take on "
              "their account are these tools: %s. You have no other way to change anything. "
              "Never say you have changed something unless one of these tools returned a "
              "successful result for that change. If none of them can do what the customer "
              "asks, say plainly that you can't do it from here and suggest the app instead. "
              "A tool result that starts with \"Refused\" means the company's rules do not "
              "allow that action through this assistant: it is a decision, not a fault. Your "
              "reply must then start by saying clearly that you can't make this change here, "
              "and point them to the app or an advisor. Never say something went wrong, never "
              "suggest trying again, never call the same tool again for it, and do not offer "
              "other changes you have not checked.")

MCP_REMINDER = ("Your only tools right now are: %s. Anything they cannot do has not been "
                "done: never say otherwise, say plainly that you can't do it from here.")

# Sign-ins in flight for the MCP cards that need one, by OAuth state: the
# PKCE verifier and where to return. Lost on restart, like any login page.
_PENDING_LOGINS = {}
MAX_PENDING_LOGINS = 100
# The MCP session each chat has open, by (conversation, card), kept for the
# whole chat as any MCP client keeps its session. Lost on restart: the next
# turn then opens a new one.
_MCP_SESSIONS = {}
MAX_MCP_SESSIONS = 1000
# JSON-RPC request ids, never reused, since a session may span many turns.
_RPC_IDS = itertools.count(1)
# Standard OpenID scopes: always asked for (the profile and email ones let the
# provider put a readable name in the token), never shown as permissions.
OIDC_SCOPES = ["openid", "profile", "email"]

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def _merge_env_file(conf, path):
    if not (path and os.path.exists(path)):
        return
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip()


def load_config(path, secrets_path=None):
    """DEFAULTS, overridden by ../demo.env, ../secrets/idp.env, config.env, then secrets/gateway-keys.env
    (the *_API_KEY vars provision_showcases.py routes there instead of into the
    tracked config.env), overridden by the real environment."""
    conf = dict(DEFAULTS)
    _merge_env_file(conf, os.path.join(HERE, "..", "demo.env"))
    _merge_env_file(conf, os.path.join(HERE, "..", "secrets", "idp.env"))
    _merge_env_file(conf, path)
    _merge_env_file(conf, secrets_path)
    for key in list(conf):
        if os.environ.get(key):
            conf[key] = os.environ[key]
    # The files above already copy any KEY=VALUE into conf, so SHOWCASE_*
    # arrives for free; the environment needs this explicit pass because the
    # loop above only honours keys that already exist.
    for key, value in os.environ.items():
        if value and SHOWCASE_ENV_RE.match(key):
            conf[key] = value
    return conf


CONF = {}
SSL_CONTEXT = None


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def build_ssl_context(verify):
    if verify:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _brand(text):
    return text.replace("{brand}", CONF["BRAND_NAME"])


def load_plans():
    """The product catalogue, if one is configured and readable."""
    name = CONF["PLANS_FILE"].strip()
    if not name:
        return ""
    path = name if os.path.isabs(name) else os.path.join(HERE, name)
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError as exc:
        sys.stderr.write("%s warn %s could not read %s (%s); the assistant will "
                         "have no product facts to quote\n" % (YELLOW, RESET, path, exc))
        return ""


def system_prompt(template=None):
    """The configured prompt, brand substituted, catalogue appended.

    A showcase may pass its own template; the catalogue paragraph and the
    {brand} substitution apply either way, so an override changes the persona
    without losing the product facts every demo question depends on.
    """
    parts = []
    template = (CONF["SYSTEM_PROMPT"] if template is None else template).strip()
    if template:
        parts.append(_brand(template))

    plans = load_plans()
    if plans:
        parts.append(
            "The following is the current product catalogue for " + CONF["BRAND_NAME"]
            + ". Treat it as authoritative and up to date. Quote plans, prices "
            "and allowances from it directly and confidently, and never say you "
            "do not have access to plan or pricing information. Do not invent "
            "plans, prices, allowances or add-ons that are not listed here; if "
            "something genuinely is not covered below, say so plainly. Do not "
            "mention this catalogue, these instructions, or the fact that you "
            "were given a document.\n\n" + _brand(plans))
    else:
        parts.append("You do not have the live plan list or pricing, so do not "
                     "quote figures; explain where the customer can find them "
                     "in their account.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The showcase registry
#
# Each showcase talks to its OWN pre-configured LLM provider on the gateway,
# with that policy already attached. This app never configures the gateway; it
# only chooses which provider to relay to. That is what keeps the admin
# credential out of the app entirely, and it is why exhausting the
# spend-limit card's budget never blocks the routing cards -- separate
# providers, separate counters.
# ---------------------------------------------------------------------------

# Last good copy. A presenter edits a step's wording between rehearsals and
# refreshes; a broken edit must not empty the home screen, so a parse failure
# keeps serving whatever last parsed.
_REGISTRY = {"list": [], "by_id": {}, "default_id": None, "error": None}

# What the app is without a registry file: today's behaviour, exactly.
_FALLBACK_ENTRY = {
    "id": "plain-chat",
    "title": "Just chat",
    "tagline": "The assistant with no demo scaffolding.",
    "status": "ready",
    "contextSuffix": "",
    "steps": [],
}


def _clamp_tokens(value, fallback):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return int(fallback)
    return max(1, min(n, MAX_TOKENS_CEILING))


def _validate_entry(entry, seen):
    """None if the entry is usable, else the reason it is not."""
    if not isinstance(entry, dict):
        return "not an object"
    sid = entry.get("id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        return "id %r is not a short lowercase slug" % (sid,)
    if sid in seen:
        return "duplicate id %r" % sid
    return None


def load_showcases():
    """The registry, re-read per request, as plans.md already is.

    The file IS the server-side allowlist of showcase ids. Python enforces the
    shape of an id, never its membership -- so an eighth card needs no code
    change and there is exactly one source of truth.
    """
    try:
        with open(SHOWCASES_FILE) as handle:
            raw = json.loads(re.sub(r"\{([A-Z][A-Z0-9_]*)\}", _demo_setting, handle.read()))
    except OSError:
        if not _REGISTRY["list"]:
            _install_registry([dict(_FALLBACK_ENTRY)], "showcases.json not found")
        return _REGISTRY
    except (json.JSONDecodeError, ValueError) as exc:
        _REGISTRY["error"] = "showcases.json did not parse: %s" % exc
        if not _REGISTRY["list"]:
            _install_registry([dict(_FALLBACK_ENTRY)], _REGISTRY["error"])
        return _REGISTRY

    if not isinstance(raw, list):
        _REGISTRY["error"] = "showcases.json must be a JSON array"
        return _REGISTRY

    good, seen, problems = [], set(), []
    disabled = _disabled_cards()
    for entry in raw:
        reason = _validate_entry(entry, seen)
        if reason:
            problems.append(reason)
            continue
        seen.add(entry["id"])
        if entry["id"] in disabled:
            entry = dict(entry, status="disabled", blockedBy=[DISABLED_REASON])
        good.append(entry)

    if not good:
        _REGISTRY["error"] = "; ".join(problems) or "showcases.json is empty"
        if not _REGISTRY["list"]:
            _install_registry([dict(_FALLBACK_ENTRY)], _REGISTRY["error"])
        return _REGISTRY

    _install_registry(good, "; ".join(problems) or None)
    return _REGISTRY


# Cards listed in DISABLED_CARDS (demo.env) stay on the home screen, greyed
# out and not clickable, with this reason -- for a card whose outside service
# is down during a live demo.
DISABLED_REASON = "Not available in this demo"


def _disabled_cards():
    """Card ids in DISABLED_CARDS, from the environment, else demo.env. Read
    per request, like showcases.json, so a card goes off or on again with no
    restart."""
    path = ROOT / "demo.env"
    value = os.environ.get("DISABLED_CARDS") or \
        (read_env_file(path).get("DISABLED_CARDS", "") if path.exists() else "")
    return {card.strip() for card in value.split(",") if card.strip()}


# The settings a card's text can show as {NAME}: those of demo.env.example,
# none of them secret.
SHOWABLE_SETTINGS = set(read_env_file(ROOT / "demo.env.example"))


def _demo_setting(match):
    """Replaces a {NAME} of showcases.json with its demo.env value, escaped so
    it stays text. Any other name is left as is."""
    value = setting(match[1]) if match[1] in SHOWABLE_SETTINGS else ""
    return json.dumps(value)[1:-1] if value else match[0]


def _install_registry(entries, error):
    ids = [e["id"] for e in entries]
    _REGISTRY["list"] = entries
    _REGISTRY["by_id"] = {e["id"]: e for e in entries}
    # Array order is card order; the default is plain-chat when present so a
    # client that sends no showcase behaves exactly as it did before.
    _REGISTRY["default_id"] = "plain-chat" if "plain-chat" in ids else ids[0]
    _REGISTRY["error"] = error


# ---- gateway URL resolution ------------------------------------------------

def _valid_url(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def gateway_url(entry=None):
    """The gateway a card calls: the MCP one for an MCP card."""
    return (MCP_GATEWAY if (entry or {}).get("mcp") else LLM_GATEWAY).url


def _env_key(showcase_id, field, provider_id=DEFAULT_PROVIDER_ID):
    """showcase_id + field (+ provider_id, for any provider but the default
    one) -> the config.env variable name, e.g. "semantic-routing" + "CONTEXT"
    -> SHOWCASE_SEMANTIC_ROUTING_CONTEXT for the default provider, or
    SHOWCASE_SEMANTIC_ROUTING_<PROVIDER>_CONTEXT for any other. Mirrors
    scripts/python/provision_showcases.py's env_key exactly -- the two must
    agree, since that script is what writes these vars. The default provider
    gets no segment, so every existing config.env keeps working untouched."""
    key = "SHOWCASE_%s" % showcase_id.upper().replace("-", "_")
    if provider_id != DEFAULT_PROVIDER_ID:
        key += "_" + provider_id.upper()
    return "%s_%s" % (key, field)


def _sc_conf(showcase_id, field, provider_id=DEFAULT_PROVIDER_ID):
    return CONF.get(_env_key(showcase_id, field, provider_id), "").strip()


def default_model(provider_id=DEFAULT_PROVIDER_ID):
    """The model sent when nothing else overrides it. --model aside, this is
    always the provider's own fast/cheap model from providers.json -- the
    default provider gets no special case, unlike the context/model-role
    lookups below which still must (see _env_key)."""
    return MODEL_OVERRIDE or PROVIDERS[provider_id]["models"]["small"]


def _resolve_model_role(role, provider_id):
    """showcases.json's overrides.model names a role ("small"/"large"), never
    a literal model id, so it resolves correctly under whichever provider is
    active -- same reasoning as the frontend's modelForRole() (screens.js)."""
    return PROVIDERS[provider_id]["models"].get(role) if role else None


def resolve_gateway(entry, provider_id=DEFAULT_PROVIDER_ID):
    """(url, source) for one showcase on one provider; (None, reason) when
    it is not wired.

    Nothing here ever reads the request body: the caller has already turned a
    client-supplied id into a registry entry via a dict lookup.
    """
    sid = entry["id"]
    suffix = PROVIDERS[provider_id]["messagesSuffix"]

    pinned = _sc_conf(sid, "URL", provider_id)
    if pinned:
        if not _valid_url(pinned):
            return None, "%s is not an http(s) URL" % _env_key(sid, "URL", provider_id)
        return pinned, _env_key(sid, "URL", provider_id)

    # Skipped by the provisioning: a gateway setting is missing.
    needs = missing_gateway_settings(entry)
    if needs:
        return None, "needs " + needs

    # A card with its own "route" talks to one LlmProxy spanning two
    # providers (route.defaultProvider + route.routedProvider), whatever the
    # provider dropdown says, in defaultProvider's wire format. Unwired when
    # either one is disabled in providers.json.
    route = entry.get("route")
    if route:
        ctx = route.get("context") or ""
        if not CONTEXT_RE.match(ctx):
            return None, "route.context %r is not a plain path" % ctx
        disabled = [p for p in (route.get("defaultProvider"), route.get("routedProvider")) if p not in PROVIDERS]
        if disabled:
            return None, "route needs %s, disabled in providers.json" % " and ".join(map(str, disabled))
        return (gateway_url() + ctx.rstrip("/") + PROVIDERS[route["defaultProvider"]]["messagesSuffix"],
                "showcases.json route")

    # An MCP card's url is its MCP proxy's endpoint. Its model is
    # mcp.provider's plain-chat lane, in the OpenAI format the tool loop
    # speaks (see Handler._mcp_turn).
    mcp = entry.get("mcp")
    if mcp:
        ctx = mcp.get("context") or ""
        if not CONTEXT_RE.match(ctx):
            return None, "mcp.context %r is not a plain path" % ctx
        provider = mcp.get("provider")
        if provider not in PROVIDERS:
            return None, "mcp needs %s, disabled in providers.json" % provider
        if PROVIDERS[provider]["wire"] != "openai":
            return None, "mcp.provider %s must speak the OpenAI format" % provider
        return gateway_url(entry) + ctx.rstrip("/") + "/mcp", "showcases.json mcp"

    # The context for any (showcase, provider) pair is always exactly
    # /<providerId>-<contextSuffix>, or bare /<providerId> when contextSuffix
    # is empty -- scripts/python/provision_showcases.py builds the gateway's own
    # lane contexts with this identical formula (see its build_lanes()). A
    # showcase with no contextSuffix at all is simply unwired, for any
    # provider including the default one: there is no other fallback.
    context_suffix = entry.get("contextSuffix")
    if context_suffix is None:
        return None, "no provider configured"
    if provider_id in (entry.get("unsupportedProviders") or []):
        return None, "%s's gateway route can't carry this capability" % provider_id
    ctx = f"/{provider_id}-{context_suffix}" if context_suffix else f"/{provider_id}"
    if not CONTEXT_RE.match(ctx):
        return None, "computed context %r is not a plain path" % ctx
    return gateway_url() + ctx.rstrip("/") + suffix, "showcases.json contextSuffix"


def showcase_context(url, provider_id=DEFAULT_PROVIDER_ID, entry=None):
    """The provider path, host stripped -- safe to publish to the browser."""
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path
    route = (entry or {}).get("route")
    if (entry or {}).get("mcp"):
        suffix = "/mcp"
    else:
        suffix = PROVIDERS[route["defaultProvider"] if route else provider_id]["messagesSuffix"]
    return path.split(suffix)[0] or "/"


def launcher_links():
    """The demo's other web consoles for the launcher menu, grouped. The
    observability ones follow ANALYTICS_MODE, as run-local.sh does. A console
    that is not set up yet has no url and says how to set it up. Same host
    and ports as demo.env, so the links work locally and on the VM."""
    host = CONF.get("DEMO_HOSTNAME") or "localhost"
    groups = [{"name": "Platform", "links": [
        {"name": "AI Workspace", "note": "Proxies, providers and their policies",
         "url": "https://%s:%s/ai-workspace/" % (host, CONF.get("AI_WORKSPACE_PORT", "9643"))},
        {"name": "API Portal", "note": "The developer portal",
         "url": "https://%s:%s/api-portal/" % (host, CONF.get("API_PORTAL_PORT", "9543"))}]}]
    if CONF.get("ANALYTICS_MODE", "moesif") == "local":
        observe = [{"name": name, "note": note, "url": "http://%s:%s/" % (host, port)}
                   for name, note, port in (("Grafana", "Metrics dashboards", 3000),
                                            ("Jaeger", "Traces of each request", 16686),
                                            ("OpenSearch Dashboards", "Gateway logs", 5601),
                                            ("Prometheus", "Raw metrics", 9092))]
    else:
        keys = {}
        _merge_env_file(keys, os.path.join(HERE, "..", "secrets", "api-keys.env"))
        observe = [{"name": "Moesif", "note": "API analytics for every gateway call",
                    "url": "https://www.moesif.com/wrap"
                    if keys.get("APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID") else None,
                    "setup": "Add the Moesif application id to secrets/api-keys.env"}]
    groups.append({"name": "Observability", "links": observe})
    groups.append({"name": "Identity", "links": [
        {"name": "Identity provider", "note": "Users, roles and the sign-in app",
         "url": CONF.get("IDP_CONSOLE_URL") or None,
         "setup": "Set IDP_CONSOLE_URL in secrets/idp.env"}]})
    return groups


def resolve_showcase(showcase_id, provider_id=DEFAULT_PROVIDER_ID):
    """Everything one request needs, with global config as the floor.

    The single place per-showcase and global config compose, so the request
    path stays branch-free and there is one thing to read when a demo lands on
    the wrong model.
    """
    entry = _REGISTRY["by_id"][showcase_id]
    url, source = resolve_gateway(entry, provider_id)
    wiring = entry.get("gateway") or {}
    overrides = entry.get("overrides") or {}
    # A routed or MCP card (see resolve_gateway) speaks its own provider's
    # wire format and default model, and its env vars carry the default
    # provider's (unsuffixed) names, since it belongs to no single provider.
    mcp = entry.get("mcp")
    own_provider = (entry.get("route") or {}).get("defaultProvider") or (mcp or {}).get("provider")
    conf_provider = DEFAULT_PROVIDER_ID if own_provider else provider_id
    wire_provider = own_provider if own_provider and url else provider_id

    fallback_model = default_model(wire_provider)
    model = (MODEL_OVERRIDE
             or _sc_conf(showcase_id, "MODEL", conf_provider)
             or _resolve_model_role(overrides.get("model"), wire_provider)
             or fallback_model)
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", str(model)):
        model = fallback_model

    tokens = (_sc_conf(showcase_id, "MAX_TOKENS", conf_provider)
              or overrides.get("maxTokens")
              or CONF["MAX_TOKENS"])

    prompt = _sc_conf(showcase_id, "SYSTEM_PROMPT", conf_provider) or overrides.get("systemPrompt")

    # Streaming is on everywhere except where a policy cannot cope with it.
    # semantic-cache v1.1.0 never stores a streamed response -- measured on this
    # gateway: three identical stream:true calls all missed, and a later
    # non-streamed call for the same prompt missed too, so nothing had been
    # stored at all. Overriding streamingJsonPath does not help. That card has
    # to send stream:false or it can never show a hit.
    stream = wiring.get("stream")
    env_stream = _sc_conf(showcase_id, "STREAM", conf_provider)
    if env_stream:
        stream = _truthy(env_stream)
    if stream is None:
        stream = True

    context = showcase_context(url, provider_id, entry)
    own_key = _sc_conf(showcase_id, "API_KEY", conf_provider)
    # MCP proxies take no key (the platform issues keys for LLM resources only).
    fallback_key = CONF["GATEWAY_API_KEY"] if conf_provider == DEFAULT_PROVIDER_ID and not mcp else ""
    selector = entry.get("selector")
    return {
        "id": showcase_id,
        "title": entry.get("title") or showcase_id,
        "provider": provider_id,
        "wire": PROVIDERS[wire_provider]["wire"],
        "url": url,
        "source": source,
        "context": context,
        # gateway_url() + the raw context path, no messagesSuffix -- what a
        # wire whose URL isn't just showcase["url"] (Gemini: the model and
        # the operation are both part of the path) builds the real request
        # URL from. None when there's no context at all (unwired).
        "baseUrl": (gateway_url(entry) + context) if context else None,
        # A routed card with a selector names no model: the gateway picks the
        # provider and the model from the selector's header.
        "model": None if (entry.get("route") and selector) else str(model),
        "selector": selector,
        # What a 429 means on this card, when it is not the cost budget.
        "limit": (entry.get("gateway") or {}).get("limit"),
        "stream": bool(stream),
        "maxTokens": _clamp_tokens(tokens, CONF["MAX_TOKENS"]),
        "systemPrompt": system_prompt(prompt),
        "blockStatus": (entry.get("gateway") or {}).get("blockStatus"),
        "status": entry.get("status") or "ready",
        # Per-lane gateway key. api-key-auth issues keys per LLM proxy and a
        # key is rejected on any other proxy -- verified on this gateway, so
        # one key for all lanes is not available and each needs its own.
        # GATEWAY_API_KEY remains the fallback for the default provider's
        # plain-chat.
        "apiKey": own_key or fallback_key,
        # "showcase" (its own SHOWCASE_*_API_KEY), "fallback" (GATEWAY_API_KEY)
        # or None. A fallback key is plain-chat's, so on any other lane it
        # will 401 -- /health and the banner surface this instead of letting
        # "keyed" look healthy.
        "keySource": ("showcase" if own_key else "fallback" if fallback_key
                      else "open" if mcp else None),
    }


def cost_meta(headers):
    """Pull the cost-budget headers the llm-cost-based-ratelimit policy emits.

    Absent on a 429 -- the gateway stops reporting the balance once it starts
    refusing -- so the UI must treat missing values as "unknown", not zero.
    """
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    return {
        "costLimit": lower.get("x-ratelimit-cost-limit-dollars"),
        "costRemaining": lower.get("x-ratelimit-cost-remaining-dollars"),
    }


# Response headers the gateway may set that the browser has no business
# seeing. x-api-key is the gateway's OWN auth header name -- a misconfigured
# set-headers policy echoing it would otherwise be forwarded to the page purely
# for starting with x-.
META_HEADER_DENY = re.compile(
    r"(key|token|secret|auth|credential|passw|signature|cookie|session)", re.I)
META_HEADER_NAME = re.compile(r"^x-[a-z0-9._-]+$")
META_MAX_HEADERS = 40
META_MAX_VALUE = 256

# Best-effort readings of policies that are not released yet. The raw headers
# are the contract; these are a convenience layer allowed to be absent or
# wrong. None always means "unknown", never "miss".
CACHE_HIT_HEADERS = ("x-cache", "x-cache-status", "x-cache-hit",
                     "x-semantic-cache", "x-semantic-cache-status",
                     "x-semantic-cache-hit", "x-wso2-cache-hit")
CACHE_HIT_VALUES = ("hit", "true", "1", "yes")


def forwarded_headers(headers):
    """Every x-* response header, bounded, minus anything credential-shaped."""
    seen = {}
    for name, value in (headers.items() if headers else ()):
        low = name.lower()
        if not META_HEADER_NAME.match(low) or META_HEADER_DENY.search(low):
            continue
        # HTTPMessage is a multidict: the same name can legitimately repeat.
        clean = "".join(c for c in str(value) if c >= " " or c == "\t").strip()
        seen[low] = (seen[low] + ", " + clean) if low in seen else clean

    truncated = len(seen) > META_MAX_HEADERS
    kept = {}
    for name in sorted(seen)[:META_MAX_HEADERS]:
        kept[name] = seen[name][:META_MAX_VALUE]
        truncated = truncated or len(seen[name]) > META_MAX_VALUE
    return kept, truncated


def gateway_meta(headers, streamed=True):
    """cost_meta, plus every other x-* the gateway chose to tell us.

    Additive on purpose: costLimit and costRemaining stay exactly where the
    page already reads them, so a client that knows nothing about the rest
    keeps working unchanged.
    """
    meta = cost_meta(headers)
    kept, truncated = forwarded_headers(headers)
    meta["headers"] = kept
    meta["headersTruncated"] = truncated
    meta["cost"] = kept.get("x-llm-cost")
    meta["costStatus"] = kept.get("x-llm-cost-status")
    meta["streamed"] = streamed
    meta["cacheHit"] = None
    for name in CACHE_HIT_HEADERS:
        if name in kept:
            meta["cacheHit"] = kept[name].strip().lower() in CACHE_HIT_VALUES
            break
    return meta


def guardrail_verdict(raw):
    """True when a guardrail replaced the model's answer with its verdict. One
    that steps in after the gateway has started the reply can only swap the
    body: the status stays 200."""
    try:
        body = json.loads(raw)
    except ValueError:
        return False
    message = body.get("message") if isinstance(body, dict) else None
    return isinstance(message, dict) and message.get("action") == "GUARDRAIL_INTERVENED"


def block_direction(raw):
    """REQUEST or RESPONSE: which side a guardrail refused, when it says so."""
    try:
        message = json.loads(raw).get("message")
    except (ValueError, AttributeError):
        return None
    return message.get("direction") if isinstance(message, dict) else None


def classify_gateway_error(status, headers, body, showcase):
    """Did the gateway refuse on purpose, or did it break?

    A guardrail block is the SUCCESS case on the prompt-injection card, so it
    must not read as a fault. None of these signals is authoritative -- the
    block policies set no documented marker yet -- so gatewayStatus and
    gatewayBody are always passed through untouched alongside.
    """
    if status == 429:
        return "budget_exhausted"

    # Primary signal, and the honest one: it encodes what the operator knows
    # about that provider, not a guess about a policy schema. On a provider
    # whose only request-phase policy is the guardrail, a 400 is a block.
    if showcase.get("blockStatus") and status == showcase["blockStatus"]:
        return "policy_block"

    lower = {k.lower(): v for k, v in (headers or {}).items()}
    for marker in _split_conf("POLICY_BLOCK_HEADERS"):
        if marker.lower() in lower:
            return "policy_block"

    haystack = (body or "")[:4000].lower()
    for marker in _split_conf("POLICY_BLOCK_MARKERS"):
        if marker.lower() in haystack:
            return "policy_block"

    return "upstream_error"


def _split_conf(key):
    return [v.strip() for v in CONF.get(key, "").split(",") if v.strip()]


def sanitise(messages):
    """Keep only well-formed user/assistant turns carrying text.

    The client never sends thinking blocks back, so there is no signature to
    preserve here -- see the README for why that matters.
    """
    clean = []
    for message in messages[-MAX_MESSAGES:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        clean.append({"role": role, "content": content})
    return clean


def mcp_needs_signin(entry):
    return any(p.get("name") == "mcp-auth" for p in ((entry.get("mcp") or {}).get("policies") or []))


def token_cookie(showcase_id):
    """One token per card: each MCP proxy is its own protected resource."""
    return "mcp_token_" + showcase_id.replace("-", "_")


def jwt_claims(token):
    """The token's claims, for display only -- unverified here, since the
    gateway is what checks the signature."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None


def _fetch_json(url):
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=15) as response:
        return json.loads(response.read())


def protected_resource_metadata(www_authenticate):
    """What any MCP client reads after a 401: the protected resource metadata
    (RFC 9728) its WWW-Authenticate header points to, naming the
    authorization server and the scopes."""
    match = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "")
    if not match:
        raise ValueError("no resource_metadata in the gateway's WWW-Authenticate")
    return _fetch_json(match.group(1))


def mcp_authorization_server(prm):
    """The authorization server's own metadata: its endpoints."""
    issuer = (prm.get("authorization_servers") or [None])[0]
    if not issuer:
        raise ValueError("the protected resource metadata names no authorization server")
    parts = urllib.parse.urlsplit(issuer)
    for url in (issuer.rstrip("/") + "/.well-known/openid-configuration",
                "%s://%s/.well-known/oauth-authorization-server%s" % (parts.scheme, parts.netloc, parts.path)):
        try:
            return _fetch_json(url)
        except (urllib.error.URLError, ValueError):
            continue
    raise ValueError("no authorization server metadata for %s" % issuer)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AiChatbot/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s[chatbot]%s %s\n" % (DIM, RESET, fmt % args))

    # -- small response helpers -------------------------------------------

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _begin_sse(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-accel-buffering", "no")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

    def _chunk(self, text):
        """One HTTP chunk. Flushed immediately so the UI paints as it arrives."""
        data = text.encode("utf-8")
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _end_chunks(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _sse(self, event, payload):
        self._chunk("event: %s\ndata: %s\n\n" % (event, json.dumps(payload)))

    # -- GET ---------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        provider_id = (query.get("provider") or [DEFAULT_PROVIDER_ID])[0]
        if provider_id not in PROVIDERS:
            provider_id = DEFAULT_PROVIDER_ID

        if path in ("/health", "/healthz"):
            registry = load_showcases()
            wiring = {}
            for entry in registry["list"]:
                url, source = resolve_gateway(entry, provider_id)
                resolved = resolve_showcase(entry["id"], provider_id)
                wiring[entry["id"]] = {
                    "status": entry.get("status") or "ready",
                    "stream": (entry.get("gateway") or {}).get("stream") is not False,
                    # Full URLs here are fine -- the server binds 127.0.0.1.
                    "url": url,
                    "source": source,
                    # Never the key itself, only whether one is wired -- this
                    # endpoint is the first place a missing key shows up as a
                    # 401, so the boolean earns its place.
                    "keyed": bool(resolved["apiKey"]),
                    # "fallback" next to keyed:true is plain-chat's key standing
                    # in for a lane that has none of its own -- expect a 401.
                    "keySource": resolved["keySource"],
                }
            return self._json(200, {
                "status": "ok",
                "provider": provider_id,
                "gatewayBase": gateway_url(),
                "defaultModel": default_model(provider_id),
                "maxTokens": int(CONF["MAX_TOKENS"]),
                "tlsVerify": _truthy(CONF["TLS_VERIFY"]),
                "gatewayApiKeySet": bool(CONF["GATEWAY_API_KEY"]) or any(
                resolve_showcase(s, provider_id)["apiKey"] for s in _REGISTRY["by_id"]),
                "brand": CONF["BRAND_NAME"],
                "catalogue": bool(load_plans()),
                "registry": {"file": os.path.basename(SHOWCASES_FILE),
                             "count": len(registry["list"]),
                             "error": registry["error"]},
                "showcases": wiring,
            })

        if path == "/api/providers":
            return self._json(200, {
                "default": DEFAULT_PROVIDER_ID,
                "providers": [{"id": pid, "displayName": p["displayName"], "models": p["models"],
                               "costBudget": p["costBudget"]}
                              for pid, p in PROVIDERS.items()],
            })
        if path == "/api/showcases":
            return self._json(200, self._registry_payload(provider_id))
        if path in ("/oauth/login", "/oauth/callback", "/oauth/logout", "/api/session"):
            return self._signin(path, query)
        if path == "/api/config":
            # No model list: the gateway decides which model answers, and the
            # page reads that decision back off the response instead.
            return self._json(200, {
                "defaultModel": default_model(provider_id),
                "brand": CONF["BRAND_NAME"],
                "launcher": launcher_links()})
        return self._serve_static(path)

    def _cookie(self, name):
        jar = http.cookies.SimpleCookie(self.headers.get("cookie") or "")
        return jar[name].value if name in jar else None

    def _redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("location", location)
        if cookie:
            self.send_header("set-cookie", cookie)
        self.send_header("content-length", "0")
        self.end_headers()

    def _signin(self, path, query):
        """Sign-in for the MCP cards whose proxy runs mcp-auth: authorization
        code + PKCE against whichever authorization server the proxy's
        metadata names, so IDP_CLIENT_ID is the only setting. The access token
        stays in an HttpOnly cookie, per card, and goes on that card's MCP
        calls only -- never to the model."""
        arg = lambda key: (query.get(key) or [""])[0]
        if path == "/oauth/callback":
            pending = _PENDING_LOGINS.pop(arg("state"), None)
            if not pending:
                return self._json(400, {"error": "unknown or expired sign-in, start again from the card"})
            if arg("error") or not arg("code"):  # cancelled or refused at the identity provider
                return self._redirect("/#/" + pending["card"])
            body = urllib.parse.urlencode({
                "grant_type": "authorization_code", "code": arg("code"),
                "redirect_uri": pending["redirect_uri"], "client_id": CONF.get("IDP_CLIENT_ID", ""),
                "code_verifier": pending["verifier"]}).encode()
            request = urllib.request.Request(pending["token_endpoint"], data=body, headers={
                "content-type": "application/x-www-form-urlencoded", "accept": "application/json"})
            try:
                with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=15) as response:
                    token = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return self._json(502, {"error": "the identity provider refused the code",
                                        "detail": exc.read().decode("utf-8", "replace")[:500]})
            except (urllib.error.URLError, ValueError) as exc:
                return self._json(502, {"error": "cannot reach the identity provider",
                                        "detail": str(getattr(exc, "reason", exc))})
            return self._redirect("/#/" + pending["card"], "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
                token_cookie(pending["card"]), token["access_token"], int(token.get("expires_in") or 3600)))

        card = arg("card")
        entry = load_showcases()["by_id"].get(card) if ID_RE.match(card) else None
        if not entry or not mcp_needs_signin(entry):
            return self._json(400, {"error": "not an MCP card that needs a sign-in", "card": card})
        if path == "/oauth/logout":
            return self._redirect("/#/" + card, "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % token_cookie(card))
        if path == "/api/session":
            claims = jwt_claims(self._cookie(token_cookie(card)))
            if not claims or claims.get("exp", 0) < time.time():
                return self._json(200, {"signedIn": False})
            return self._json(200, {
                "signedIn": True,
                "user": claims.get("preferred_username") or claims.get("username") or claims.get("email")
                        or claims.get("sub"),
                "scopes": [s for s in (claims.get("scope") or "").split() if s not in OIDC_SCOPES]})

        if not CONF.get("IDP_CLIENT_ID"):
            return self._json(503, {"error": "IDP_CLIENT_ID is not set in secrets/idp.env"})
        try:
            # As any MCP client: call the proxy, follow its 401.
            session = {}
            self._mcp_rpc(gateway_url(entry) + entry["mcp"]["context"].rstrip("/") + "/mcp", "tools/list", {}, session)
            prm = protected_resource_metadata((session.get("headers") or {}).get("www-authenticate"))
            meta = mcp_authorization_server(prm)
        except (urllib.error.URLError, ValueError, KeyError) as exc:
            return self._json(502, {"error": "cannot discover the authorization server",
                                    "detail": str(getattr(exc, "reason", exc))})
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(16)
        redirect_uri = "http://%s/oauth/callback" % (self.headers.get("host") or "localhost:%s" % CONF["AI_CHATBOT_PORT"])
        if len(_PENDING_LOGINS) >= MAX_PENDING_LOGINS:
            _PENDING_LOGINS.clear()
        _PENDING_LOGINS[state] = {"verifier": verifier, "card": card, "redirect_uri": redirect_uri,
                                  "token_endpoint": meta["token_endpoint"]}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        scopes = OIDC_SCOPES + [s for s in prm.get("scopes_supported") or [] if s not in OIDC_SCOPES]
        return self._redirect(meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": CONF["IDP_CLIENT_ID"], "redirect_uri": redirect_uri,
            "scope": " ".join(scopes), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
            # Always ask for credentials, so a demo can switch users without the
            # identity provider's own session signing the previous one back in.
            "prompt": "login"}))

    def _registry_payload(self, provider_id=DEFAULT_PROVIDER_ID):
        """The registry as the browser sees it, for one provider.

        Carries the provider PATH but never the host, and never a resolved URL:
        the client selects a showcase by id, and this server owns the mapping.
        """
        registry = load_showcases()
        out = []
        for entry in registry["list"]:
            url, source = resolve_gateway(entry, provider_id)
            # A card whose lane was never provisioned has no gateway key of its
            # own, and every call would be refused: show it as not wired.
            if url and not entry.get("mcp") \
                    and resolve_showcase(entry["id"], provider_id)["keySource"] != "showcase":
                url, source = None, "not provisioned on this gateway yet, see the README"
            item = dict((k, v) for k, v in entry.items() if k != "gateway")
            item["configured"] = bool(url)
            item["streaming"] = (entry.get("gateway") or {}).get("stream") is not False
            item["gatewayContext"] = showcase_context(url, provider_id, entry)
            item["unwiredReason"] = None if url else source
            out.append(item)
        return {"brand": CONF["BRAND_NAME"],
                "provider": provider_id,
                "defaultShowcase": registry["default_id"],
                "registry": {"error": registry["error"]},
                "showcases": out}

    def _serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.realpath(os.path.join(STATIC, rel))
        if not target.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(target):
            return self._json(404, {"error": "not found"})
        kinds = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".ico": "image/x-icon"}
        ctype = kinds.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- POST --------------------------------------------------------------

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/__shutdown__":
            # The stop/restart path for this server: no lsof, no pidfile, no
            # kill signal -- scripts/stop-ai-chatbot.sh asks the process
            # itself to exit over loopback HTTP, the same way any other
            # route here is reached. Safe to call from inside a handler
            # specifically because this is a ThreadingHTTPServer: shutdown()
            # blocks until serve_forever() (running on the main thread)
            # notices and returns, and that's a different thread than this
            # one, so there's no self-deadlock. Threaded so the response
            # below actually reaches the client before the socket goes away.
            self._json(200, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/api/cache/clear":
            return self._clear_cache()
        if path != "/api/chat":
            return self._json(404, {"error": "not found"})

        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._json(400, {"error": "invalid content-length header"})
        # length < 0 matters beyond "malformed": Python's rfile.read(-1) means
        # "read until EOF", which hangs this thread forever on a client that
        # never closes -- confirmed live. Every other negative value raises
        # inside the try below and is already handled; -1 alone needs its own
        # guard because it's a valid argument to read(), just not to us.
        if length < 0:
            return self._json(400, {"error": "invalid content-length header"})
        if length > MAX_BODY_BYTES:
            return self._json(413, {"error": "conversation too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError, RecursionError):
            return self._json(400, {"error": "body is not valid JSON"})

        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            return self._json(400, {"error": "messages must be a list"})
        messages = sanitise(raw_messages)
        if not messages:
            return self._json(400, {"error": "no usable messages in request"})

        raw_provider = payload.get("provider")
        if raw_provider is None or raw_provider == "":
            provider_id = DEFAULT_PROVIDER_ID
        elif not isinstance(raw_provider, str) or raw_provider not in PROVIDERS:
            return self._json(400, {"error": "unknown provider", "provider": raw_provider,
                                    "known": sorted(PROVIDERS)})
        else:
            provider_id = raw_provider

        # The showcase id selects from this server's own registry and is used
        # as a dict key and NOTHING else (see ID_RE above). A relay that
        # forwarded a client-supplied URL would hand back exactly the direct
        # gateway reach the browser is denied for (see module docstring).
        registry = load_showcases()
        raw = payload.get("showcase")
        if raw is None or raw == "":
            showcase_id = registry["default_id"]
        elif not isinstance(raw, str) or not ID_RE.match(raw):
            return self._json(400, {"error": "showcase must be a short id string"})
        else:
            showcase_id = raw

        if showcase_id not in registry["by_id"]:
            # Loud, never a silent fall-through to the default: a typo that
            # quietly answers from the plain provider is a demo that lies.
            return self._json(400, {"error": "unknown showcase",
                                    "showcase": showcase_id,
                                    "known": sorted(registry["by_id"])})

        showcase = resolve_showcase(showcase_id, provider_id)
        if not showcase["url"]:
            # 503, not 502: nothing upstream was contacted or misbehaved --
            # this server has no provider for this card.
            return self._json(503, {
                "error": "this showcase has no gateway provider configured",
                "showcase": showcase_id,
                "provider": provider_id,
                "gatewayOutcome": "not_configured",
                "hint": showcase["source"]})

        entry = registry["by_id"][showcase_id]
        if entry.get("mcp"):
            conversation = payload.get("conversation")
            if not isinstance(conversation, str) or not CONVERSATION_RE.match(conversation):
                conversation = None
            token = self._cookie(token_cookie(showcase_id)) if mcp_needs_signin(entry) else None
            return self._mcp_turn(showcase, messages, conversation, token)

        prompt = showcase["systemPrompt"]
        wire = WIRES.get(showcase["wire"])
        if wire is None:
            # Can only happen if PROVIDERS names a "wire" with no matching
            # entry here -- a config mistake, not a request problem.
            return self._json(500, {"error": "showcase configured for an unknown wire format",
                                    "wire": showcase["wire"], "showcase": showcase_id})
        url, upstream, headers = wire["buildUpstream"](showcase, messages, prompt)
        # Deliberately no upstream-provider key here -- the gateway holds
        # that. This is the gateway-side api-key-auth credential, per lane.
        if showcase["apiKey"]:
            headers[CONF["GATEWAY_API_KEY_HEADER"]] = showcase["apiKey"]
        # The selector stands in for a claim of the signed-in customer's token
        # (tier, region, id). Only the card's own values reach the gateway;
        # anything else is the first one.
        selector = showcase["selector"]
        if selector:
            choice = payload.get("choice")
            headers[selector["header"]] = choice if choice in selector["values"] else selector["values"][0]

        request = urllib.request.Request(
            url, data=json.dumps(upstream).encode(),
            headers=headers, method="POST")

        try:
            response = urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=180)
        except urllib.error.HTTPError as exc:
            # The gateway's own words, verbatim. A 401 means the provider's key
            # is wrong; a 404 means the model is not registered; a 429 is a
            # rate-limit policy doing its job. Never paper over these.
            body = exc.read().decode("utf-8", "replace")
            self.log_message("[%s] gateway HTTP %d: %s",
                             showcase_id, exc.code, body[:400])
            outcome = classify_gateway_error(exc.code, exc.headers, body, showcase)
            payload = {"error": "gateway rejected the request",
                       "gatewayStatus": exc.code,
                       "gatewayOutcome": outcome,
                       "showcase": showcase_id,
                       "gatewayBody": body[:4000]}
            payload.update(gateway_meta(exc.headers))
            if outcome == "budget_exhausted" and showcase["limit"]:
                payload["error"] = showcase["limit"]["message"]
                payload["limitTitle"] = showcase["limit"]["title"]
            elif outcome == "budget_exhausted":
                payload["error"] = ("the gateway refused this call: the "
                                    "cost budget for this provider is exhausted")
            elif outcome == "policy_block" and block_direction(body) == "RESPONSE":
                payload["error"] = "a gateway policy blocked the model's answer"
            elif outcome == "policy_block":
                payload["error"] = ("a gateway policy blocked this request "
                                    "before any model was called")
            # A policy refusing on purpose is not a fault. 422 says "the
            # content was rejected" without the alarm of a 502, and the client
            # still branches on gatewayStatus, so a 422 carrying 429 renders
            # exactly as it did before.
            status = 422 if outcome in ("policy_block", "budget_exhausted") else 502
            return self._json(status, payload)
        except urllib.error.URLError as exc:
            self.log_message("[%s] cannot reach gateway: %s", showcase_id, exc.reason)
            return self._json(502, {"error": "cannot reach the gateway",
                                    "gatewayOutcome": "unreachable",
                                    "showcase": showcase_id,
                                    "detail": str(exc.reason)})

        ctype = (response.headers.get("content-type") or "").lower()
        streamed = "text/event-stream" in ctype
        if not streamed:
            raw = response.read()
            response.close()
            if guardrail_verdict(raw):
                self.log_message("[%s] a guardrail replaced the answer: %s", showcase_id, raw[:400])
                payload = {"error": "a gateway policy blocked the model's answer",
                           "gatewayOutcome": "policy_block", "showcase": showcase_id,
                           "gatewayBody": raw.decode("utf-8", "replace")[:4000]}
                payload.update(gateway_meta(response.headers))
                return self._json(422, payload)
            headers, response = response.headers, io.BytesIO(raw)
            response.headers = headers
        relay = wire["relayStream" if streamed else "relayWhole"]
        try:
            relay(self, response)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("client went away mid-stream")
        finally:
            response.close()

    def _relay_stream(self, response):
        """Forward the gateway's SSE line by line, unbuffered."""
        self._begin_sse()
        self._sse("gateway_meta", gateway_meta(response.headers))
        while True:
            line = response.readline()
            if not line:
                break
            self._chunk(line.decode("utf-8", "replace"))
        self._end_chunks()

    def _relay_whole(self, response):
        """Fallback: the gateway answered with one JSON body, not a stream.

        Synthesised into the same SSE shape so the client has one code path.
        """
        raw = response.read().decode("utf-8", "replace")
        self._begin_sse()
        # streamed=False: the SSE below is synthesised from a body already in
        # hand, so a time-to-first-token measured here would be timing this
        # relay, not the gateway. The UI suppresses that chip.
        self._sse("gateway_meta", gateway_meta(response.headers, streamed=False))
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._sse("gateway_error", {"error": "unparseable gateway response",
                                        "body": raw[:4000]})
            return self._end_chunks()

        self._sse("message_start", {"type": "message_start", "message": message})
        for index, block in enumerate(message.get("content") or []):
            if block.get("type") != "text":
                continue
            self._sse("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": block.get("text", "")}})
        self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": message.get("stop_reason")},
            "usage": message.get("usage") or {}})
        self._sse("message_stop", {"type": "message_stop"})
        self._end_chunks()

    # -- OpenAI-compatible wire (Mistral et al.) ----------------------------
    #
    # Both methods below emit the EXACT same internal event shape as the two
    # above (message_start / content_block_delta / message_delta /
    # message_stop), just built from an OpenAI-shaped response instead of an
    # Anthropic one. That shape is the client's only contract (see static/
    # chat.js) -- neither it nor anything else in this file needs to know a
    # second wire format exists.

    @staticmethod
    def _usage_from_openai(usage):
        """{prompt_tokens, completion_tokens} -> {input_tokens, output_tokens}."""
        usage = usage or {}
        return {"input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens")}

    def _relay_stream_openai(self, response):
        """Forward an OpenAI-style SSE stream (`data: {...}` lines, ending in
        `data: [DONE]`, no named `event:` lines) translated into our shape."""
        self._begin_sse()
        self._sse("gateway_meta", gateway_meta(response.headers))
        started = False
        finish_reason = None
        usage = {}
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue
            if not started:
                self._sse("message_start", {"type": "message_start", "message": {
                    "id": chunk.get("id"), "model": chunk.get("model"), "usage": {}}})
                started = True
            choice = (chunk.get("choices") or [{}])[0]
            text = (choice.get("delta") or {}).get("content")
            if text:
                self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text}})
            finish_reason = choice.get("finish_reason") or finish_reason
            usage = chunk.get("usage") or usage
        self._sse("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": finish_reason},
                                    "usage": self._usage_from_openai(usage)})
        self._sse("message_stop", {"type": "message_stop"})
        self._end_chunks()

    def _relay_whole_openai(self, response):
        """Fallback: one OpenAI-shaped JSON body, not a stream."""
        raw = response.read().decode("utf-8", "replace")
        self._begin_sse()
        self._sse("gateway_meta", gateway_meta(response.headers, streamed=False))
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._sse("gateway_error", {"error": "unparseable gateway response",
                                        "body": raw[:4000]})
            return self._end_chunks()
        self._emit_openai(body)

    def _emit_openai(self, body):
        """One OpenAI-shaped reply as our event shape, then the end."""
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = self._usage_from_openai(body.get("usage"))
        self._sse("message_start", {"type": "message_start", "message": {
            "id": body.get("id"), "model": body.get("model"), "usage": usage}})
        text = message.get("content") or ""
        if text:
            self._sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text}})
        self._sse("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": choice.get("finish_reason")},
                                    "usage": usage})
        self._sse("message_stop", {"type": "message_stop"})
        self._end_chunks()

    # -- MCP cards -------------------------------------------------------------
    #
    # The app is the agent: it lists the MCP proxy's tools, offers them to the
    # model, and makes every tool call the model asks for through that proxy,
    # so the gateway's MCP policies apply to each one. Not streamed: the turn
    # is run to the end, then replayed in the usual event shape, preceded by
    # one "tool_call" event per call.

    def _mcp_turn(self, showcase, messages, conversation, token):
        mcp_url = showcase["url"]
        llm = resolve_showcase("plain-chat", _REGISTRY["by_id"][showcase["id"]]["mcp"]["provider"])
        if not llm["url"]:
            return self._json(503, {"error": "the agent's model has no gateway lane",
                                    "showcase": showcase["id"], "gatewayOutcome": "not_configured",
                                    "hint": "plain-chat: " + llm["source"]})

        # The chat's MCP session if it has one, else a new one: the server's
        # state and the gateway's per-session limits last for the whole chat.
        key = (conversation, showcase["id"])
        session = {"sid": _MCP_SESSIONS.get(key) if conversation else None, "token": token}
        status = 0
        if session["sid"]:
            status, reply = self._mcp_rpc(mcp_url, "tools/list", {}, session)
        if status != 200:  # no session yet, or one the server no longer knows
            session["sid"] = None
            status, reply = self._mcp_rpc(mcp_url, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ai-chatbot", "version": "1.0"}}, session)
            if status == 200:
                self._mcp_rpc(mcp_url, "notifications/initialized", None, session)
                status, reply = self._mcp_rpc(mcp_url, "tools/list", {}, session)
        if status == 200 and conversation and session.get("sid"):
            if len(_MCP_SESSIONS) >= MAX_MCP_SESSIONS:
                _MCP_SESSIONS.clear()
            _MCP_SESSIONS[key] = session["sid"]
        if status == 401:
            # Shown the way an MCP client reads it: the 401 points to the
            # proxy's protected resource metadata, which names the identity
            # provider and the scopes. The client then asks the user to sign in
            # (here: Sign in, see _signin) -- nothing logs in by itself.
            try:
                prm = protected_resource_metadata(session["headers"].get("www-authenticate"))
                learned = ("An MCP client reads the 401's WWW-Authenticate header, then the protected resource metadata:\n"
                           "  authorization server  %s\n  scopes                %s\n"
                           "and asks the user to sign in there."
                           % (", ".join(prm.get("authorization_servers") or []), " ".join(prm.get("scopes_supported") or [])))
            except (urllib.error.URLError, ValueError):
                learned = json.dumps(reply)[:4000]
            return self._json(401, {"error": "this MCP server needs a signed-in user",
                                    "gatewayStatus": 401, "gatewayOutcome": "signin_required",
                                    "showcase": showcase["id"], "gatewayBody": learned,
                                    "headers": {"www-authenticate": session["headers"].get("www-authenticate")}})
        if status != 200 or "result" not in reply:
            return self._json(502, {"error": "the MCP proxy did not list its tools",
                                    "gatewayStatus": status or None,
                                    "gatewayOutcome": "error" if status else "unreachable",
                                    "showcase": showcase["id"], "gatewayBody": json.dumps(reply)[:4000]})
        tools = [{"type": "function", "function": {
                     "name": t["name"], "description": t.get("description", ""),
                     "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
                 for t in reply["result"].get("tools") or []]

        # The tool list is the gateway's answer, so it already reflects any
        # tool a policy has hidden -- the model is told exactly what it has.
        names = ", ".join(t["function"]["name"] for t in tools) or "none"
        convo = [{"role": "system", "content": showcase["systemPrompt"] + "\n\n" + MCP_PROMPT % names}] + messages
        # Repeated last, where a small model weighs it most: without it, the
        # agent sometimes reports a change no tool made.
        convo.append({"role": "system", "content": MCP_REMINDER % names})
        headers = {"content-type": "application/json", "accept": "application/json"}
        if llm["apiKey"]:
            headers[CONF["GATEWAY_API_KEY_HEADER"]] = llm["apiKey"]
        calls, usage = [], {"prompt_tokens": 0, "completion_tokens": 0}
        results, must_answer = {}, False
        for round_ in range(MCP_MAX_ROUNDS):
            upstream = {"model": showcase["model"], "max_tokens": showcase["maxTokens"], "messages": convo}
            if tools:
                upstream["tools"] = tools
                # A model going round in circles, or out of rounds, answers
                # with what it has instead of ending the turn with no text.
                must_answer = must_answer or round_ == MCP_MAX_ROUNDS - 1
                if must_answer:
                    upstream["tool_choice"] = "none"
            request = urllib.request.Request(llm["url"], data=json.dumps(upstream).encode(),
                                             headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=180) as response:
                    response_headers, body = response.headers, json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                self.log_message("[%s] gateway HTTP %d: %s", showcase["id"], exc.code, text[:400])
                outcome = classify_gateway_error(exc.code, exc.headers, text, llm)
                return self._json(422 if outcome in ("policy_block", "budget_exhausted") else 502, {
                    "error": "the agent's model call was rejected", "gatewayStatus": exc.code,
                    "gatewayOutcome": outcome, "showcase": showcase["id"], "gatewayBody": text[:4000]})
            except (urllib.error.URLError, ValueError) as exc:
                return self._json(502, {"error": "no usable reply from the agent's model",
                                        "gatewayOutcome": "unreachable", "showcase": showcase["id"],
                                        "detail": str(getattr(exc, "reason", exc))})
            for key in usage:
                usage[key] += (body.get("usage") or {}).get(key) or 0
            message = (body.get("choices") or [{}])[0].get("message") or {}
            if not message.get("tool_calls") or must_answer:
                break
            convo.append({"role": "assistant", "content": message.get("content") or "",
                          "tool_calls": message["tool_calls"]})
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    args = {}
                key = (function.get("name"), json.dumps(args, sort_keys=True))
                if key in results:  # asked again: same answer, no second call
                    must_answer = True
                else:
                    event = self._mcp_tool_call(mcp_url, function.get("name"), args, session)
                    calls.append(event)
                    results[key] = event["result"]
                convo.append({"role": "tool", "tool_call_id": call.get("id"),
                              "name": function.get("name"), "content": results[key]})

        body["usage"] = usage  # every round's tokens, not just the last one's
        self._begin_sse()
        # The tools the gateway listed, per MCP server, as it listed them: a
        # tool a policy hides is visibly missing, even when the agent calls nothing.
        entry = _REGISTRY["by_id"][showcase["id"]]["mcp"]
        self._sse("tool_list", {"servers": [{"name": entry.get("server") or entry["context"],
                                             "tools": [t["function"]["name"] for t in tools]}]})
        for event in calls:
            self._sse("tool_call", event)
        self._sse("gateway_meta", gateway_meta(response_headers, streamed=False))
        self._emit_openai(body)

    def _clear_cache(self):
        """Empties the gateway's semantic cache, for the Clear cache button.
        Only this machine's gateway: a remote one's cache is out of reach."""
        # The body is read even though nothing in it is used: left unread, it
        # would be taken for the start of the next request on the connection.
        try:
            self.rfile.read(min(max(int(self.headers.get("content-length") or 0), 0), 1024))
        except ValueError:
            return self._json(400, {"error": "invalid content-length header"})
        # Sent by the page as JSON: a plain form on another site cannot.
        if not (self.headers.get("content-type") or "").startswith("application/json"):
            return self._json(415, {"error": "send the request as application/json"})
        if not LLM_GATEWAY.local:
            return self._json(409, {"error": "the gateway runs elsewhere: its cache cannot be cleared from here"})
        try:
            done = subprocess.run([str(ROOT / "scripts" / "clear-cache.sh")], capture_output=True, text=True,
                                  timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._json(502, {"error": "could not clear the cache", "detail": str(exc)})
        if done.returncode != 0:
            return self._json(502, {"error": "could not clear the cache", "detail": done.stderr[-400:]})
        # The script prints how many cached answers it deleted.
        out = done.stdout.split()
        return self._json(200, {"cleared": int(out[-1]) if out and out[-1].isdigit() else None})

    def _mcp_tool_call(self, url, name, args, session):
        """One tools/call through the MCP proxy, as a tool_call event: what the
        UI shows, and in "result" what the model is told."""
        status, reply = self._mcp_rpc(url, "tools/call", {"name": name, "arguments": args}, session)
        error = reply.get("error")
        # A gateway policy may say which argument broke which rule.
        data = error.get("data") if isinstance(error, dict) else None
        reasons = "; ".join("%s %s" % (e.get("path", ""), e.get("message", ""))
                            for e in (data.get("errors") or [] if isinstance(data, dict) else []))
        error = error.get("message") if isinstance(error, dict) else reply.get("message") or error
        if reasons:
            error = "%s (%s)" % (error, reasons)
        result = reply.get("result") or {}
        if status == 200 and not error:
            text = "\n".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
            outcome = "tool_error" if result.get("isError") else "ok"
        elif status == 200:
            text, outcome = "The tool failed: %s" % error, "tool_error"
        else:
            # Worded as a decision: a small model reads "failed" as a fault and
            # tells the customer to try again.
            text = ("Refused by the gateway: a rule, not a fault. Do not retry. "
                    "(HTTP %s: %s)" % (status, error or "no detail"))
            outcome = "rate_limited" if status == 429 else "refused" if status in (400, 401, 403) else "error"
        return {"name": name, "arguments": args, "gatewayStatus": status,
                "outcome": outcome, "result": text[:4000]}

    def _mcp_rpc(self, url, method, params, session):
        """One JSON-RPC message to an MCP proxy: (HTTP status, reply). params
        None sends a notification, which gets no reply. The reply may come as
        JSON or as an event stream; both are read."""
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message.update(id=next(_RPC_IDS), params=params)
        headers = {"content-type": "application/json",
                   "accept": "application/json, text/event-stream",
                   "mcp-protocol-version": "2025-06-18"}
        if session.get("sid"):
            headers["mcp-session-id"] = session["sid"]
        if session.get("token"):
            headers["authorization"] = "Bearer " + session["token"]
        request = urllib.request.Request(url, data=json.dumps(message).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=30) as response:
                status, raw = response.status, response.read().decode("utf-8", "replace")
                session["sid"] = response.headers.get("mcp-session-id") or session.get("sid")
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read().decode("utf-8", "replace")
            session["headers"] = exc.headers
        except urllib.error.URLError as exc:
            return 0, {"error": {"message": str(exc.reason)}}
        data = [line[5:] for line in raw.splitlines() if line.startswith("data:")]
        try:
            reply = json.loads(data[-1] if data else raw) if raw.strip() else {}
        except ValueError:
            reply = {"error": {"message": raw[:300]}}
        return status, reply if isinstance(reply, dict) else {}

    # -- Gemini's native wire -----------------------------------------------
    #
    # Neither Anthropic-shaped nor OpenAI-shaped: model is in the URL, not
    # the body (see _build_upstream_gemini); the reply is
    # candidates[0].content.parts[].text, not content/choices; roles are
    # user/model, not user/assistant. Both methods still emit the exact same
    # internal event shape as every other wire.

    @staticmethod
    def _usage_from_gemini(usage):
        usage = usage or {}
        return {"input_tokens": usage.get("promptTokenCount"),
                "output_tokens": usage.get("candidatesTokenCount")}

    def _relay_stream_gemini(self, response):
        """Forward Gemini's SSE stream (`data: {...}` lines, no [DONE]
        marker -- the connection just closes). Each chunk's parts[].text is
        incremental, not the whole reply so far -- confirmed directly: a
        multi-chunk reply's chunks concatenate to the full text, they don't
        repeat it."""
        self._begin_sse()
        self._sse("gateway_meta", gateway_meta(response.headers))
        started = False
        finish_reason = None
        usage = {}
        model_version = None
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            try:
                chunk = json.loads(line[len("data:"):].strip())
            except (json.JSONDecodeError, ValueError):
                continue
            model_version = chunk.get("modelVersion") or model_version
            if not started:
                self._sse("message_start", {"type": "message_start", "message": {
                    "id": chunk.get("responseId"), "model": model_version, "usage": {}}})
                started = True
            candidate = (chunk.get("candidates") or [{}])[0]
            for part in (candidate.get("content") or {}).get("parts") or []:
                text = part.get("text")
                if text:
                    self._sse("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": text}})
            finish_reason = candidate.get("finishReason") or finish_reason
            usage = chunk.get("usageMetadata") or usage
        self._sse("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": finish_reason},
                                    "usage": self._usage_from_gemini(usage)})
        self._sse("message_stop", {"type": "message_stop"})
        self._end_chunks()

    def _relay_whole_gemini(self, response):
        """Fallback: one Gemini-shaped JSON body (:generateContent, not
        :streamGenerateContent), not a stream."""
        raw = response.read().decode("utf-8", "replace")
        self._begin_sse()
        self._sse("gateway_meta", gateway_meta(response.headers, streamed=False))
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._sse("gateway_error", {"error": "unparseable gateway response",
                                        "body": raw[:4000]})
            return self._end_chunks()

        candidate = (body.get("candidates") or [{}])[0]
        usage = self._usage_from_gemini(body.get("usageMetadata"))
        self._sse("message_start", {"type": "message_start", "message": {
            "id": body.get("responseId"), "model": body.get("modelVersion"), "usage": usage}})
        text = "".join(part.get("text", "")
                       for part in (candidate.get("content") or {}).get("parts") or [])
        if text:
            self._sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": text}})
        self._sse("message_delta", {"type": "message_delta",
                                    "delta": {"stop_reason": candidate.get("finishReason")},
                                    "usage": usage})
        self._sse("message_stop", {"type": "message_stop"})
        self._end_chunks()


# What PROVIDERS' "wire" values dispatch to: how to build the upstream
# request (returns (url, body, headers); most wires just return
# showcase["url"] -- see _build_upstream_gemini for the one that doesn't)
# and which Handler method relays the response back in this app's one
# internal event shape. Defined here, after the class, so these are the
# actual unbound methods rather than name strings resolved via getattr() at
# request time -- a typo now fails at import, not on the first request that
# hits it. A new wire format needs one more entry here (see the PROVIDERS
# comment above); everything else is just a new providers.json entry.
WIRES = {
    "anthropic": {"buildUpstream": _build_upstream_anthropic,
                  "relayStream": Handler._relay_stream, "relayWhole": Handler._relay_whole},
    "openai": {"buildUpstream": _build_upstream_openai,
               "relayStream": Handler._relay_stream_openai, "relayWhole": Handler._relay_whole_openai},
    "gemini": {"buildUpstream": _build_upstream_gemini,
               "relayStream": Handler._relay_stream_gemini, "relayWhole": Handler._relay_whole_gemini},
}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(HERE, "config.env"))
    parser.add_argument("--secrets", default=os.path.join(HERE, "..", "secrets", "gateway-keys.env"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--model")
    args = parser.parse_args()

    global CONF, SSL_CONTEXT, MODEL_OVERRIDE
    CONF = load_config(args.config, args.secrets)
    if args.model:
        # See MODEL_OVERRIDE above: stomps the default on every card.
        MODEL_OVERRIDE = args.model

    verify = _truthy(CONF["TLS_VERIFY"])
    SSL_CONTEXT = build_ssl_context(verify)
    port = args.port or int(CONF["AI_CHATBOT_PORT"])

    registry = load_showcases()

    print("%s%s AI assistant%s" % (BOLD, CONF["BRAND_NAME"], RESET))
    print("  base     %s" % LLM_GATEWAY.url)
    if MCP_GATEWAY.url != LLM_GATEWAY.url:
        print("  mcp base %s" % MCP_GATEWAY.url)
    print("  registry %s (%d showcases, default %s)"
          % (os.path.basename(SHOWCASES_FILE), len(registry["list"]),
             registry["default_id"]))
    if registry["error"]:
        print("  %swarn%s     %s" % (YELLOW, RESET, registry["error"]))
    print("  catalog  %s" % ("%s (%d chars)" % (CONF["PLANS_FILE"], len(load_plans()))
                              if load_plans() else "none - the assistant will not quote prices"))
    print("  model    %s  (max_tokens %s)" % (default_model(), CONF["MAX_TOKENS"]))
    print("           %sa default only - the gateway's routers decide which "
          "model answers%s" % (DIM, RESET))
    sources = {s: resolve_showcase(s)["keySource"] for s in _REGISTRY["by_id"]}
    keyed = sum(1 for src in sources.values() if src)
    print("  key      %s" % ("gateway-side key on %d/%d showcases, header %s"
                             % (keyed, len(_REGISTRY["by_id"]), CONF["GATEWAY_API_KEY_HEADER"])
                             if keyed else "none sent - the gateway holds the Anthropic key"))
    # GATEWAY_API_KEY is plain-chat's own key: any other lane falling back to
    # it will be refused, so name them rather than count them as keyed.
    borrowed = sorted(s for s, src in sources.items() if src == "fallback" and s != "plain-chat")
    if borrowed:
        print("           %sGATEWAY_API_KEY fallback only (expect 401): %s%s"
              % (YELLOW, ", ".join(borrowed), RESET))

    print("\n  %sshowcases%s" % (BOLD, RESET))
    hosts, wired = set(), 0
    for entry in registry["list"]:
        url, source = resolve_gateway(entry)
        base_host = urllib.parse.urlsplit(gateway_url(entry)).netloc
        ready = (entry.get("status") or "ready") == "ready"
        if url:
            wired += 1
            hosts.add(urllib.parse.urlsplit(url).netloc)
            # A card whose policy is not attached but which IS wired will
            # answer normally and prove nothing -- the failure mode most likely
            # to embarrass someone in front of a customer.
            tag = ("%sok     %s" % (GREEN, RESET) if ready
                   else "%scheck  %s" % (YELLOW, RESET))
            off = ("  %s(off-base)%s" % (YELLOW, RESET)
                   if urllib.parse.urlsplit(url).netloc != base_host else "")
            # The reply arrives in one piece on this card -- the text won't
            # type out, and someone will notice.
            nostream = ("  %s(no streaming)%s" % (DIM, RESET)
                        if (entry.get("gateway") or {}).get("stream") is False
                        else "")
            print("    %s %-20s %-24s %s%s%s"
                  % (tag, entry["id"], showcase_context(url, entry=entry), source, off, nostream))
        else:
            # "ready" and unwired is an operator bug: this was supposed to work.
            tag = ("%sMISSING%s" % (RED, RESET) if ready
                   else "%sunwired%s" % (DIM, RESET))
            reason = (entry.get("blockedBy") or [source])[0]
            print("    %s %-20s %-24s %s" % (tag, entry["id"], "-", reason))

    unwired = len(registry["list"]) - wired
    print("\n  %d of %d wired.%s" % (wired, len(registry["list"]),
          " The other %d load their card and refuse to chat." % unwired
          if unwired else ""))

    if not verify:
        print("  %swarn%s     TLS verification is OFF (TLS_VERIFY=false) for "
              "%d host%s: %s"
              % (YELLOW, RESET, len(hosts) or 1, "" if len(hosts) == 1 else "s",
                 ", ".join(sorted(hosts)) or base_host))
    print("\n  %s->%s  http://localhost:%d\n" % (GREEN, RESET, port))

    try:
        # Loopback only, deliberately. /health publishes the resolved provider
        # URLs, so widening this bind would make internal topology readable to
        # anything on the network.
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
