"""Shared control-plane client: the HTTP/auth plumbing and provider registry
used by both provision_showcases.py (provisioning) and failover_control.py
(live demo fault-injection toggles). Import, don't execute.

Everything specific to the lane catalogue (showcases.json/lane_templates) or
ai-chatbot integration (config.env, secrets/gateway-keys.env) stays in
provision_showcases.py -- this module only knows the Platform API itself and
the provider registry (providers.json). One exception: budget_duration() below
reads a single field out of showcases.json, since that's the same kind of
plain config read this module already does (DEMO_ENV, PROVIDERS), and the
value must agree with what the UI displays.
"""
import base64
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # scripts/python/platform_api.py -> repo root


def _read_env_file(path: Path) -> dict:
    """Flat KEY=VALUE format shared by demo.env, ai-gateway/.env, etc.
    Parsed by hand since this is one of two consumers that aren't bash."""
    conf = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        conf[key.strip()] = value.strip()
    return conf


def _read_demo_env() -> dict:
    """This machine's settings from demo.env: the demo's name, hostname,
    versions and ports. On the first run, demo.env is created from
    demo.env.example, as the shell scripts do."""
    path, example = ROOT / "demo.env", ROOT / "demo.env.example"
    if not path.exists():
        if not example.exists():
            sys.exit(f"missing {example} -- did you mean to run this from a checkout of the repo, "
                     "not a subdirectory copy?")
        path.write_text(example.read_text())
        print(f"  created {path.name} from {example.name} -- adapt it to this machine if needed")
    return _read_env_file(path)


DEMO_ENV = _read_demo_env()

def _setting(name: str) -> str:
    """A target setting from the environment, else from demo.env, else ""."""
    return os.environ.get(name) or DEMO_ENV.get(name) or ""


# AIW_URL / GATEWAY_ID / GATEWAY_ENDPOINT (env vars or demo.env) retarget every
# script here at an already-running deployment instead of this repo's own
# compose stacks -- see provision_showcases.py's --existing-gateway. Unset,
# they fall back to the local-dev values below. ADMIN_PASSWORD is read from
# the environment only: demo.env is not a secrets file.
AIW = _setting("AIW_URL") or f"https://localhost:{DEMO_ENV['AI_WORKSPACE_PORT']}"  # ai-workspace BFF, see ai-workspace/docker-compose.yaml
AIW = AIW.rstrip("/")
ADMIN_USERNAME = _setting("ADMIN_USERNAME") or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")  # seeded by scripts/generate-local-certs.sh

GATEWAY_ID = _setting("GATEWAY_ID") or "local-gateway"  # immutable resource id, not the display name


def gateway_ports() -> dict:
    """The gateway's ports, each moved up by the port offset from demo.env."""
    offset = int(DEMO_ENV.get("GATEWAY_PORT_OFFSET") or 0)
    return {key: str(int(value) + offset)
            for key, value in _read_env_file(ROOT / "ai-gateway" / ".env").items()
            if key.startswith("GATEWAY_") and key.endswith("_PORT") and value.isdigit()}


GATEWAY_ENDPOINT = _setting("GATEWAY_ENDPOINT") or f"localhost:{gateway_ports()['GATEWAY_HTTPS_PORT']}"

# MCP proxies can go to a gateway of their own, apart from the LLM traffic.
# Unset, they share the gateway above.
MCP_GATEWAY_ID = _setting("MCP_GATEWAY_ID") or GATEWAY_ID
MCP_GATEWAY_ENDPOINT = _setting("MCP_GATEWAY_ENDPOINT") or GATEWAY_ENDPOINT

# The gateway's own management API: asks the gateway what it holds without
# sending traffic through it, so the checks leave nothing in analytics. Same
# admin account as AI Workspace. Known for this repo's own gateway; for
# another one, set GATEWAY_MGMT_URL, or the checks probe the gateway instead.
GATEWAY_MGMT = (os.environ.get("GATEWAY_MGMT_URL")
                or ("" if os.environ.get("GATEWAY_ENDPOINT")
                    else f"http://localhost:{gateway_ports()['GATEWAY_CONTROLLER_REST_PORT']}")).rstrip("/")

# One entry per upstream LLM, loaded from providers.json at the repo root --
# not defined here, because ai-chatbot/server.py needs the exact same
# records (it reads id/displayName/wire/messagesSuffix/models; this script
# additionally reads secretVar/template/upstreamUrl/authHeader/
# authValuePrefix/requestModel), and a provider/model mismatch between two
# separate copies was a real, repeated source of bugs. To add a provider:
# one entry in providers.json, its key added to secrets/api-keys.env, and
# (only if its wire format isn't "anthropic", "openai" or "gemini") a
# request/response translator in server.py's WIRES dict -- nothing else
# changes. Set "enabled": false to drop a provider from both this script and
# ai-chatbot without deleting its config.
#
# template/upstreamUrl/authHeader/authValuePrefix/requestModel came from GET
# /llm-provider-templates/{id} (docs/rest-apis/platform-api/
# llm-provider-templates.md); messagesSuffix from each provider's own
# OpenAPI spec (github.com/wso2/api-platform/llm-provider-specs). costBudget
# is spend-limit/cost-based-routing's $-per-1h budget, hand-tuned per
# provider against ai-gateway/configs/llm-pricing/model_prices.json (see
# budget_for() below) -- flat across providers would be meaningless, since
# one provider's tokens can cost two orders of magnitude more than
# another's.
def load_providers() -> list:
    path = ROOT / "providers.json"
    if not path.exists():
        sys.exit(f"missing {path} — the shared provider registry this script and "
                  "ai-chatbot/server.py both read")
    try:
        providers = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    required = {"id", "displayName", "enabled", "wire", "messagesSuffix", "models",
                "requestModel", "contentPath", "secretVar", "template", "upstreamUrl",
                "authHeader", "authValuePrefix", "costBudget"}
    for p in providers:
        missing = required - p.keys()
        if missing:
            sys.exit(f"{path}: provider {p.get('id', '?')!r} is missing field(s): {sorted(missing)}")
        small, large = p["models"].get("small"), p["models"].get("large")
        if not small or not large:
            sys.exit(f"{path}: provider {p['id']!r} needs both models.small and models.large set")
        if small == large:
            sys.exit(f"{path}: provider {p['id']!r} has models.small == models.large ({small!r}) -- "
                      "every two-model showcase (cost-based-routing, time-based-routing, model-failover) "
                      "would silently misbehave with no real fallback to route to; if this provider "
                      "genuinely only has one usable model, exclude it from those showcases instead via "
                      "showcases.json's unsupportedProviders")
    providers = [p for p in providers if p["enabled"]]
    if not providers:
        sys.exit(f"{path}: every provider has \"enabled\": false — nothing to provision")
    # Derived, not stored -- mechanically computable from id/displayName, so
    # keeping them as separate JSON fields would just be the drift-prone
    # redundancy providers.json exists to remove.
    for p in providers:
        # Default provider (providers[0]) keeps unprefixed lane ids and env
        # var names, so already-deployed resources and an existing
        # config.env keep working untouched.
        p["lanePrefix"] = "" if p["id"] == providers[0]["id"] else f"{p['id']}-"
        # Optional "providerId" renames only the LlmProvider resource (and
        # its context), for a control plane where another LlmProvider
        # already holds the id. Lanes, env vars and transformers keep "id".
        p["providerId"] = p.get("providerId") or p["id"]
        p["providerContext"] = f"/{p['providerId']}"
        p["appId"] = f"ai-chatbot-{p['id']}"
        p["appDisplayName"] = f"AI Chatbot ({p['displayName']})"
        p["appDescription"] = f"Public chatbot showcase — single key for all the {p['displayName']} lanes"
    return providers


PROVIDERS = load_providers()
DEFAULT_PROVIDER_ID = PROVIDERS[0]["id"]           # unprefixed lane ids/env vars belong to this one

SECRETS_FILE = ROOT / "secrets" / "api-keys.env"   # one file, every provider's key — see providers.json's secretVar


def budget_for(provider: dict) -> float:
    """spend-limit/cost-based-routing's $-per-1h budget for this provider --
    providers.json's own costBudget, hand-tuned to $0.02 for Anthropic
    against claude-opus-4-6's $0.000025/output-token price in
    ai-gateway/configs/llm-pricing/model_prices.json (about 800 output
    tokens, roughly one detailed catalogue answer). Mistral and Gemini's
    large models are priced far differently there -- confirmed:
    ministral-14b-latest is ~125x cheaper per output token than
    claude-opus-4-6, so a flat $0.02 bought ~12 full-length replies instead
    of about one, never tripping the wall -- so their values use the same
    800-token baseline against each provider's own price. ai-chatbot/server.py
    exposes this field via /api/providers for the frontend's budget meter
    (costBudgetFor() in screens.js, used by spend.js) -- one number, not a
    second copy to keep in sync."""
    return provider["costBudget"]


def showcases() -> list:
    """ai-chatbot/showcases.json, the card catalogue."""
    return json.loads((ROOT / "ai-chatbot" / "showcases.json").read_text())


def showcase(showcase_id: str) -> dict:
    """One showcases.json entry ({} if absent)."""
    return next((s for s in showcases() if s["id"] == showcase_id), {})


def budget_duration(showcase_id: str) -> str:
    """How often one showcase's gateway budget rolls over and starts fresh
    again -- read from ai-chatbot/showcases.json's <showcase>.budget.duration,
    the same place the UI displays it, rather than a literal in
    provision_showcases.py that could drift from the page. Each budgeted
    showcase has its own window: spend-limit wants a short one (the wall
    resets quickly between rehearsals), cost-based-routing a longer one (time
    to explain the switch before the large model comes back)."""
    duration = (showcase(showcase_id).get("budget") or {}).get("duration")
    if not duration:
        sys.exit(f"ai-chatbot/showcases.json: {showcase_id}.budget.duration is missing -- "
                  f"{showcase_id}'s gateway budget window is read from there")
    return duration


# =============================================================================
# HTTP client
# =============================================================================
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_ctx),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
)


def request(method: str, url: str, data=None, headers=None):
    """Raw HTTP call against any URL — used both for the ai-workspace API
    (via api()) and for probing the gateway directly (via route_is_live())."""
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-Requested-By", "ai-workspace")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        # A connection that never answers would otherwise hang the whole run.
        with _opener.open(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw.decode(errors="replace")}
    except (urllib.error.URLError, ConnectionError, TimeoutError, ssl.SSLError) as e:
        # Transient — a container mid-restart resetting the connection, a TLS
        # handshake dropped under host CPU contention, etc. Status 0 so
        # callers that poll (wait_for_gateway_active, deploy) just treat it
        # as "not there yet" instead of crashing the whole run.
        return 0, {"status": "error", "code": "TRANSIENT_NETWORK_ERROR", "message": str(e)}


def api(method: str, path: str, data=None):
    """Call against the ai-workspace control-plane API (requires login() first)."""
    return request(method, f"{AIW}/ai-workspace/proxy/api/v0.9{path}", data)


def login(attempts: int = 5):
    for attempt in range(1, attempts + 1):
        status, resp = request("POST", f"{AIW}/ai-workspace/api/login",
                                {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
        if status == 200:
            return
        if status == 0 and attempt < attempts:  # transient — containers may still be settling
            time.sleep(2)
            continue
        sys.exit(f"login failed: {status} {resp} (admin credentials — set ADMIN_USERNAME/ADMIN_PASSWORD "
                  "env vars if you changed them from the scripts/generate-local-certs.sh default)")


def read_secret(provider: dict) -> str:
    if not SECRETS_FILE.exists():
        sys.exit(f"missing {SECRETS_FILE} — copy secrets/api-keys.env.example there and fill in real keys")
    prefix = provider["secretVar"] + "="
    for line in SECRETS_FILE.read_text().splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{provider['secretVar']} not set in {SECRETS_FILE}")


# The Content safety cards' cloud services. A service left blank here has its
# card's lanes left out, so a run without either cloud account still works.
CONTENT_SAFETY_FILE = ROOT / "secrets" / "content-safety.env"


def _content_safety_conf() -> dict:
    return _read_env_file(CONTENT_SAFETY_FILE) if CONTENT_SAFETY_FILE.exists() else {}


def azure_safety_missing() -> str:
    """What the Azure Content Safety card still needs; "" once it is set up."""
    conf = _content_safety_conf()
    endpoint = conf.get("AZURE_CONTENT_SAFETY_ENDPOINT", "")
    if conf.get("AZURE_CONTENT_SAFETY_KEY") and endpoint.startswith("https://") and "<" not in endpoint:
        return ""
    return "an Azure Content Safety endpoint and key in secrets/content-safety.env"


def bedrock_safety_missing() -> str:
    """What the AWS Bedrock card still needs; "" once it is set up."""
    conf = _content_safety_conf()
    guardrail = all(conf.get(k) for k in ("AWS_BEDROCK_REGION", "AWS_BEDROCK_GUARDRAIL_ID",
                                          "AWS_BEDROCK_GUARDRAIL_VERSION"))
    access = conf.get("AWS_BEARER_TOKEN_BEDROCK") or \
        (conf.get("AWS_ACCESS_KEY_ID") and conf.get("AWS_SECRET_ACCESS_KEY"))
    if guardrail and access:
        return ""
    return "a Bedrock guardrail and API key in secrets/content-safety.env"


def idp_missing() -> str:
    """What the MCP sign-in cards (mcp-auth) still need; "" once it is set up."""
    path = ROOT / "secrets" / "idp.env"
    conf = _read_env_file(path) if path.exists() else {}
    if all(conf.get(k) for k in ("IDP_ISSUER", "IDP_JWKS_URI", "IDP_CLIENT_ID")):
        return ""
    return "an identity provider in secrets/idp.env (see .docs/mcp-identity-provider.md)"


def gateway_verdict(status: int, body) -> str:
    """Classifies a probe response by WHO produced it, since a probe that
    gets through the gateway reaches the real upstream, whose own 400/401/404
    (an empty request, a bad upstream key) must not be read as the gateway's.
    Returns:
      "transient"     -- no HTTP answer at all (status 0)
      "no-route"      -- Envoy has no route for this context: not deployed
      "key-rejected"  -- api-key-auth refused the key (or no key was sent)
      "live"          -- the gateway matched the route and let it through
    The two gateway bodies are its own fixed replies, confirmed on this
    gateway: {"error":"Not Found"} and {"error":"Unauthorized","message":
    "Valid API key required"}."""
    if status == 0:
        return "transient"
    error = body.get("error") if isinstance(body, dict) else None
    if status == 404 and error == "Not Found":
        return "no-route"
    if status == 401 and error == "Unauthorized":
        return "key-rejected"
    return "live"


def probe(context: str, messages_suffix: str, key: str = None, endpoint: str = None) -> str:
    """One POST straight at the gateway (not the control plane), classified
    by gateway_verdict(). The body is empty on purpose: the verdict never
    depends on it, and a made-up model name would show up in analytics."""
    status, body = request("POST", f"https://{endpoint or GATEWAY_ENDPOINT}{context}{messages_suffix}",
                           {}, {"X-API-Key": key} if key else None)
    return gateway_verdict(status, body)


def route_is_live(context: str, messages_suffix: str, endpoint: str = None) -> bool:
    """Envoy matched the route -- anything but a missing route or no answer
    (a 401 for the missing key still proves the route and its chain exist)."""
    return probe(context, messages_suffix, endpoint=endpoint) in ("live", "key-rejected")


def gateway_mgmt(method: str, path: str, data=None):
    """Call against the gateway's management API (basic auth, admin account).
    Status 0 when there is none to call (see GATEWAY_MGMT)."""
    if not GATEWAY_MGMT:
        return 0, None
    token = base64.b64encode(f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}".encode()).decode()
    return request(method, f"{GATEWAY_MGMT}/api/management/v1{path}", data, {"Authorization": "Basic " + token})


def gateway_state(resource: str, resource_id: str):
    """(state, deployedAt) of a resource as the gateway holds it, ("absent",
    None) when it holds none, None when its controller can't be asked."""
    status, resp = gateway_mgmt("GET", f"/{resource}/{resource_id}")
    if status == 404:
        return ("absent", None)
    if status != 200:
        return None
    held = resp.get("status") or {}
    return (held.get("state"), held.get("deployedAt"))


def own_state(resource: str, resource_id: str, gateway_id: str = None):
    """gateway_state() on this repo's gateway; None on another one (the MCP
    gateway, when set apart), whose controller is not known here."""
    return gateway_state(resource, resource_id) if (gateway_id or GATEWAY_ID) == GATEWAY_ID else None


def on_gateway(resource: str, resource_id: str, before, context: str, messages_suffix: str,
               gateway_id: str = None, endpoint: str = None) -> bool:
    """The gateway holds a newer deployment of this resource than `before`
    (own_state() taken before deploying). Without a controller to ask, a probe
    checks that the route answers instead."""
    now = own_state(resource, resource_id, gateway_id)
    if now is None:
        return route_is_live(context, messages_suffix, endpoint)
    return now[0] == "deployed" and now != before


def deploy(resource_id: str, context: str, messages_suffix: str,
           attempts: int = 3, resource: str = "llm-proxies",
           gateway_id: str = None, endpoint: str = None):
    """Deployment is asynchronous by design (DEPLOYING -> DEPLOYED/FAILED,
    confirmed in docs/rest-apis/platform-api/llm-provider-deployments.md and
    llm-proxy-deployments.md), so this polls for a terminal status and
    retries on failure. `resource` is "llm-proxies" (default, one per lane),
    "llm-providers" (one per PROVIDERS entry) or "mcp-proxies", which pass
    the MCP gateway's gateway_id and endpoint."""
    for attempt in range(1, attempts + 1):
        before = own_state(resource, resource_id, gateway_id)
        status, resp = api("POST", f"/{resource}/{resource_id}/deployments",
                            {"name": "default", "base": "current", "gatewayId": gateway_id or GATEWAY_ID})
        if status != 201:
            sys.exit(f"  deploy failed: {status} {resp}")
        dep_id = resp["deploymentId"]
        # 30 * 2s = 60s: the controller's websocket to the control plane can
        # drop mid-deployment under host CPU contention (seen as "Heartbeat
        # timeout detected" / "Connection state changed ... reconnecting" in
        # its logs) and take 30-40s+ to reconnect before it even starts
        # processing the deployment event — a short poll window reports
        # "stuck" while it's actually just running late.
        for _ in range(30):
            time.sleep(2)
            status, resp = api("GET", f"/{resource}/{resource_id}/deployments/{dep_id}")
            if resp.get("status") in ("DEPLOYED", "FAILED"):
                break
            if on_gateway(resource, resource_id, before, context, messages_suffix, gateway_id, endpoint):
                print("  status still DEPLOYING but the gateway already holds it, proceeding")
                return
        print(f"  deploy status: {resp.get('status')} (attempt {attempt}/{attempts})")
        if resp.get("status") == "DEPLOYED":
            return
        # The control plane's own status record can get stuck on DEPLOYING
        # after a stale WebSocket ack (seen right after a gateway-controller
        # restart, logged there as "Upsert skipped (stale event)") even
        # though the runtime deploy actually completed — confirmed by curling
        # the gateway directly.
        if on_gateway(resource, resource_id, before, context, messages_suffix, gateway_id, endpoint):
            print("  status stuck at DEPLOYING but the gateway holds it, proceeding")
            return
        # A real DEPLOYMENT_TIMEOUT (route confirmed NOT live) is usually the
        # controller/runtime being starved of CPU by something else on the
        # host, not a config problem — retrying a fresh deployment attempt
        # tends to succeed once that contention passes.
        if attempt < attempts:
            print(f"  deployment failed ({resp.get('statusReason')}) — retrying")
    sys.exit(f"  deployment never reached DEPLOYED after {attempts} attempts, "
              f"and the gateway doesn't hold it either: {resp}")


# =============================================================================
# Provider lifecycle — the LlmProvider half only (one per PROVIDERS entry,
# the real upstream credential and connection). Proxy (per-showcase-lane)
# lifecycle is provision_showcases.py's own, since it needs the lane
# catalogue this module doesn't know about.
# =============================================================================
def upsert_shared_provider(provider: dict, api_key: str):
    payload = {
        "id": provider["providerId"],
        "displayName": provider["displayName"],
        "description": f"{provider['displayName']} core LLM Provider (used to fork multiple LLM Proxy)",
        "version": "v1",
        "context": provider["providerContext"],  # every LlmProxy forwards to this context
        "template": provider["template"],
        "upstream": {"main": {"url": provider["upstreamUrl"],
                               "auth": {"type": "apiKey", "header": provider["authHeader"],
                                        "value": provider["authValuePrefix"] + api_key}}},
        "modelProviders": [{"id": provider["id"], "displayName": provider["displayName"],
                             "models": [{"id": m, "displayName": m}
                                        for m in provider["models"].values()]}],
        # llm-cost lives here, once, rather than duplicated into every proxy's
        # globalPolicies — a proxy's traffic always passes through the
        # provider's own context to reach the upstream, so this still runs on
        # every showcase call.
        "globalPolicies": [{"name": "llm-cost", "version": "v1", "params": {}}],
        "operationPolicies": [], "policies": [], "openapi": "",
        # deny_all seemed safe since nothing should call this context
        # directly -- but every LlmProxy's traffic is actually routed
        # THROUGH the provider's own context to reach the upstream, so
        # deny_all 404s every proxy request too. Access control lives at the
        # proxy layer (each lane's own "security"/api-key-auth); the
        # provider itself just needs to let that already-authenticated
        # traffic through.
        "accessControl": {"mode": "allow_all"},
        "rateLimiting": {},
        "security": {"enabled": False, "apiKey": {"enabled": False, "key": "X-API-Key", "in": "header"}},
    }
    pid = provider["providerId"]
    status, _ = api("GET", f"/llm-providers/{pid}")
    if status == 200:
        status, resp = api("PUT", f"/llm-providers/{pid}", payload)
        verb = "updated"
    else:
        status, resp = api("POST", "/llm-providers", payload)
        verb = "created"
    if status not in (200, 201):
        sys.exit(f"  {provider['id']} provider {verb} failed: {status} {resp}")
    print(f"  {provider['displayName']} provider {verb}")
