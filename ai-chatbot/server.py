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
import json
import os
import re
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

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
    # scheme://host:port that every showcase's computed context path
    # (/<providerId>-<contextSuffix>, see resolve_gateway()) hangs off.
    "GATEWAY_BASE": "https://changeme.example.com:8443",
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
    "PORT": "8600",
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
    """DEFAULTS, overridden by config.env, overridden by secrets/gateway-keys.env
    (the *_API_KEY vars provision_showcases.py routes there instead of into the
    tracked config.env), overridden by the real environment."""
    conf = dict(DEFAULTS)
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
            raw = json.load(handle)
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
    for entry in raw:
        reason = _validate_entry(entry, seen)
        if reason:
            problems.append(reason)
            continue
        seen.add(entry["id"])
        good.append(entry)

    if not good:
        _REGISTRY["error"] = "; ".join(problems) or "showcases.json is empty"
        if not _REGISTRY["list"]:
            _install_registry([dict(_FALLBACK_ENTRY)], _REGISTRY["error"])
        return _REGISTRY

    _install_registry(good, "; ".join(problems) or None)
    return _REGISTRY


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


def gateway_base():
    """scheme://host:port that context paths hang off."""
    return CONF["GATEWAY_BASE"].strip().rstrip("/")


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

    # A card with its own "route" talks to one fixed LlmProxy, in one wire
    # format, whatever the provider dropdown says: a cross-provider proxy
    # (intelligent-routing -- Mistral + Anthropic behind one context) belongs
    # to no single provider, so the per-provider formula below can't name it.
    route = entry.get("route")
    if route:
        ctx = route.get("context") or ""
        if not CONTEXT_RE.match(ctx):
            return None, "route.context %r is not a plain path" % ctx
        return gateway_base() + ctx.rstrip("/") + (route.get("messagesSuffix") or ""), "showcases.json route"

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
    return gateway_base() + ctx.rstrip("/") + suffix, "showcases.json contextSuffix"


def showcase_context(url, provider_id=DEFAULT_PROVIDER_ID, entry=None):
    """The provider path, host stripped -- safe to publish to the browser."""
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path
    route = (entry or {}).get("route")
    suffix = (route.get("messagesSuffix") if route else None) or PROVIDERS[provider_id]["messagesSuffix"]
    return path.split(suffix)[0] or "/"


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
    # A routed card (see resolve_gateway) fixes its own wire and default
    # model, and its env vars carry the default provider's (unsuffixed)
    # names, since it belongs to no single provider.
    route = entry.get("route")
    conf_provider = DEFAULT_PROVIDER_ID if route else provider_id

    fallback_model = (MODEL_OVERRIDE or route["model"]) if route and route.get("model") else default_model(provider_id)
    model = (MODEL_OVERRIDE
             or _sc_conf(showcase_id, "MODEL", conf_provider)
             or (None if route else _resolve_model_role(overrides.get("model"), provider_id))
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
    fallback_key = CONF["GATEWAY_API_KEY"] if conf_provider == DEFAULT_PROVIDER_ID else ""
    return {
        "id": showcase_id,
        "title": entry.get("title") or showcase_id,
        "provider": provider_id,
        "wire": (route.get("wire") if route else None) or PROVIDERS[provider_id]["wire"],
        "url": url,
        "source": source,
        "context": context,
        # gateway_base() + the raw context path, no messagesSuffix -- what a
        # wire whose URL isn't just showcase["url"] (Gemini: the model and
        # the operation are both part of the path) builds the real request
        # URL from. None when there's no context at all (unwired).
        "baseUrl": (gateway_base() + context) if context else None,
        "model": str(model),
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
        "keySource": "showcase" if own_key else ("fallback" if fallback_key else None),
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
                "gatewayBase": gateway_base(),
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
        if path == "/api/config":
            # No model list: the gateway decides which model answers, and the
            # page reads that decision back off the response instead.
            return self._json(200, {
                "defaultModel": default_model(provider_id),
                "brand": CONF["BRAND_NAME"]})
        return self._serve_static(path)

    def _registry_payload(self, provider_id=DEFAULT_PROVIDER_ID):
        """The registry as the browser sees it, for one provider.

        Carries the provider PATH but never the host, and never a resolved URL:
        the client selects a showcase by id, and this server owns the mapping.
        """
        registry = load_showcases()
        out = []
        for entry in registry["list"]:
            url, source = resolve_gateway(entry, provider_id)
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
            if outcome == "budget_exhausted":
                payload["error"] = ("the gateway refused this call: the "
                                    "cost budget for this provider is exhausted")
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
    parser.add_argument("--gateway-base")
    parser.add_argument("--model")
    args = parser.parse_args()

    global CONF, SSL_CONTEXT, MODEL_OVERRIDE
    CONF = load_config(args.config, args.secrets)
    if args.gateway_base:
        CONF["GATEWAY_BASE"] = args.gateway_base
    if args.model:
        # See MODEL_OVERRIDE above: stomps the default on every card.
        MODEL_OVERRIDE = args.model

    verify = _truthy(CONF["TLS_VERIFY"])
    SSL_CONTEXT = build_ssl_context(verify)
    port = args.port or int(CONF["PORT"])

    registry = load_showcases()

    print("%s%s AI assistant%s" % (BOLD, CONF["BRAND_NAME"], RESET))
    print("  base     %s" % gateway_base())
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
    base_host = urllib.parse.urlsplit(gateway_base()).netloc
    hosts, wired = set(), 0
    for entry in registry["list"]:
        url, source = resolve_gateway(entry)
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
