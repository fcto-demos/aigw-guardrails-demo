#!/usr/bin/env python3
"""Single entrypoint that provisions the whole gateway side of the demo,
from a blank control plane to a fully wired ai-chatbot: registers the
gateway, then for every entry in PROVIDERS (Anthropic, Mistral, ...)
creates that provider's own LlmProvider (the real upstream credential and
connection), an LlmProxy per showcase lane on top of it (its own route,
policy chain and api-key — no separate credential or connection), deploys
everything to the gateway, groups each provider's resources under its own
GenAI Application, and writes each lane's api-key-auth key into
secrets/gateway-keys.env (gitignored — a real credential, kept out of the
tracked ai-chatbot/config.env). The gateway context each key pairs with is
never written anywhere; ai-chatbot/server.py computes it itself from
showcases.json.

Designed to run against a freshly wiped control plane (after
`docker compose down -v`) as well as an already-provisioned one — every
step checks current state first and is a no-op if it's already done. Called
by scripts/reset-and-setup.sh, which also handles everything upstream of
this (certs, the custom gateway image, bringing the containers up).

Adding a new provider is: one entry in providers.json (the repo root — read
by this script AND ai-chatbot/server.py, so there is exactly one place a
provider's models/wire/etc. are defined), its key added to
secrets/api-keys.env, and — only if its wire format isn't already one of the
three ai-chatbot/server.py's WIRES dict knows how to speak (Anthropic's
Messages API, OpenAI-style chat/completions, Gemini's generateContent) — a
request builder + response translator there.

Policy parameter schemas came from the public Policy Hub (stock policies:
llm-cost-based-ratelimit, regex-guardrail, semantic-cache, pii-masking-regex)
and from ai-gateway/policies/*/policy-definition.yaml (custom routing
policies: semantic/time/cost-based-model-routing). The REST API itself is
documented at github.com/wso2/api-platform/tree/main/docs/rest-apis/platform-api
(the published docs site doesn't render for a plain HTTP fetch — it's a JS
SPA — but the source markdown is readable straight from the repo).

Usage:
    ./scripts/python/provision_showcases.py                      # every wired lane, every provider
    ./scripts/python/provision_showcases.py spend-limit           # just Anthropic's spend-limit
    ./scripts/python/provision_showcases.py mistral-spend-limit   # just Mistral's
    ./scripts/python/provision_showcases.py model-failover        # just this lane (Anthropic)
    ./scripts/python/provision_showcases.py mistral-model-failover  # ... or Mistral's

model-failover's live fault-injection toggles (inject/break/heal/restore) are
a separate concern from provisioning -- see scripts/python/failover_control.py and
scripts/failover_demo.sh.

Reads every provider's key from secrets/api-keys.env (copy from
secrets/api-keys.env.example). Stdlib only, no pip installs, matching
ai-chatbot.

File layout (each section only calls the ones above it):
    1. Configuration — GATEWAY_DISPLAY_NAME/PROJECT_ID; everything else
       shared with scripts/python/failover_control.py lives in platform_api.py
    2. LANES — the declarative showcase catalogue, instantiated per provider
    3. Gateway registration
    4. Application / project grouping
    5. Proxy lifecycle (create, deploy, api-key) — provider lifecycle itself
       (upsert_shared_provider) is platform_api.py's
    6. ai-chatbot integration (secrets/gateway-keys.env, process restart)
    7. main()
"""
import re
import subprocess
import sys
import time

from platform_api import (
    ROOT, DEMO_ENV, GATEWAY_ID, GATEWAY_ENDPOINT, PROVIDERS, DEFAULT_PROVIDER_ID,
    request, api, login, read_secret, deploy, upsert_shared_provider, budget_for,
    BUDGET_DURATION,
)

# =============================================================================
# 1. Configuration — the only section you should need to touch to retarget
#    this script (different ports, a renamed app/gateway, a different model
#    pair).
# =============================================================================
GATEWAY_DISPLAY_NAME = "ai-gateway"
PROJECT_ID = "default"

# Gateway-issued per-lane API keys (api-key-auth), as opposed to the upstream
# provider credentials (platform_api.py's SECRETS_FILE) — a different secret,
# generated rather than pasted, but still a real credential and kept out of
# ai-chatbot/config.env for the same reason: config.env is tracked, secrets/
# is gitignored.
GATEWAY_SECRETS_FILE = ROOT / "secrets" / "gateway-keys.env"

AI_CHATBOT_LOG_FILE = ROOT / ".logs" / "ai-chatbot.log"


# =============================================================================
# 2. LANES — one LlmProxy per showcase lane, per provider, all on top of that
#    provider's single shared LlmProvider. lane_templates() is the ONE
#    declarative definition; build_lanes() instantiates it once per entry in
#    PROVIDERS, substituting that provider's models and operation path
#    (Anthropic's Messages API and Mistral's OpenAI-style chat/completions
#    disagree on almost everything except this being the one place it
#    matters: the routing policies' operationPolicies "path", and which
#    model ids get slotted into "small"/"large").
# =============================================================================
def env_key(base_id: str, field: str, provider_id: str) -> str:
    """Mirrors ai-chatbot/server.py's _env_key exactly — the two must agree,
    since this produces the var names written to secrets/gateway-keys.env
    (see write_config_env below) that server.py reads back. provider_id is
    folded in only for a non-default provider, so Anthropic's existing
    SHOWCASE_PLAIN_CHAT_API_KEY-style names are untouched."""
    key = "SHOWCASE_%s" % base_id.upper().replace("-", "_")
    if provider_id != DEFAULT_PROVIDER_ID:
        key += "_" + provider_id.upper()
    return "%s_%s" % (key, field)


def lane_templates(provider: dict) -> dict:
    """The showcase catalogue, generic across providers. Keyed by base_id —
    the showcases.json id, shared by every provider's version of that lane."""
    small, large = provider["models"]["small"], provider["models"]["large"]
    path = provider["messagesSuffix"]
    request_model = provider["requestModel"]
    budget = budget_for(provider)
    return {
        "plain-chat": {
            "context_suffix": None, "displayName": "Plain Chat", "wired": True,
            # Deliberately carries only llm-cost (added automatically for
            # every lane, at the provider level) and nothing that can block
            # -- the one lane with no policy that can refuse a request (see
            # ai-chatbot/README.md's showcase table: "none under test").
        },
        "spend-limit": {
            "context_suffix": "cost-limit", "displayName": "Spend Limit", "wired": True,
            # llm-cost-based-ratelimit only READS the cost llm-cost computes
            # (from SharedContext.Metadata["x-llm-cost"]) -- it never computes
            # its own. The provider's own global llm-cost (upsert_shared_provider)
            # runs on its own internally-forwarded request, which does not
            # carry that metadata back to the PROXY's response phase --
            # confirmed directly: two real calls through this exact lane
            # never moved x-ratelimit-cost-remaining-dollars off the full
            # limit, on EVERY provider tested (Anthropic included), not just
            # Mistral. Same reason cost-based-routing below chains its own
            # llm-cost rather than relying on the provider's.
            "globalPolicies": [{"name": "llm-cost-based-ratelimit", "version": "v1",
                                 "params": {"budgetLimits": [{"amount": budget, "duration": BUDGET_DURATION}],
                                            "consumerBased": False}},
                                {"name": "llm-cost", "version": "v1", "params": {}}],
        },
        "prompt-injection": {
            "context_suffix": "guardrail", "displayName": "Prompt Injection Guardrail", "wired": True,
            # invert:true because the policy's un-inverted default lets a request through
            # only when the regex MATCHES (an allow-list check) — we want the opposite,
            # block when it matches. ".{0,30}" between the words because a request like
            # "ignore all previous instructions" has two words between "ignore" and
            # "instructions"; a single-alternation group only catches one.
            "globalPolicies": [{"name": "regex-guardrail", "version": "v1", "params": {
                "request": {"enabled": True, "invert": True,
                            "jsonPath": provider["contentPath"],
                            "regex": r"(?i)ignore\s+.{0,30}\binstructions?\b"}}}],
        },
        "semantic-caching": {
            "context_suffix": "cache", "displayName": "Semantic Caching", "wired": True,
            "globalPolicies": [{"name": "semantic-cache", "version": "v1",
                                 "params": {"similarityThreshold": 0.95}}],
        },
        "pii-masking": {
            "context_suffix": "pii-masking", "displayName": "PII Masking", "wired": True,
            # redactPII:true (permanent "*****"), confirmed by comparing against
            # the hosted demo — with false (reversible [EMAIL_0000] placeholder),
            # the policy's own response-phase pass restores the real value before
            # it reaches the client, which defeats the showcase (verified with a
            # local echo-server upstream: request WAS masked reaching upstream,
            # but the client-facing response had the real email put back).
            "globalPolicies": [{"name": "pii-masking-regex", "version": "v1", "params": {
                "email": True, "phone": True, "ssn": True, "redactPII": True,
                "jsonPath": provider["contentPath"]}}],
        },
        "semantic-routing": {
            "context_suffix": "semantic-routing", "displayName": "Semantic Routing", "wired": True,
            # Confirmed directly in ai-gateway/policies/semantic-model-routing
            # (semanticmodelrouting.go:579): a hardcoded
            # "'requestModel.location' must be 'payload'" check, no
            # path/header/query rewriting implemented at all -- unlike the
            # other two routers below, which do support this.
            "supportedRequestModelLocations": {"payload"},
            # scorethreshold 0.75, not the schema default 0.90 — showcases.json notes
            # 0.90 measured far too strict for this catalogue's phrasing. Utterances
            # cover the "deep" semantic space (multi-factor cost comparison, roaming
            # zones, 12-month vs 30-day, data rollover) rather than the literal demo
            # question, so routing generalizes instead of overfitting one prompt.
            "operationPolicies": [{"name": "semantic-model-routing", "version": "v0", "paths": [{
                "path": path, "methods": ["POST"], "params": {
                    "contentPath": provider["contentPath"],
                    "routes": [{"model": large, "scorethreshold": 0.75, "utterances": [
                        "compare plan A and plan B",
                        "what's the difference between these two plans",
                        "which plan is better for me",
                        "I use a certain amount of data a month and travel abroad sometimes, which plan and add-ons work out cheapest over 12 months",
                        "should I go 30-day rolling or the 12-month plan for my usage",
                        "work out the cheapest option across plans including roaming charges for my trips",
                        "how does data rollover affect which plan I should pick if I travel to Zone B countries",
                        "calculate the total cost over a year for my data usage plus roaming passes",
                    ]}],
                    "defaultModel": small,
                    "requestModel": request_model,
                }}]}],
        },
        "time-based-routing": {
            "context_suffix": "time-routing", "displayName": "Time-Based Routing", "wired": True,
            # Confirmed in ai-gateway/policies/time-based-model-routing
            # (config.go/timebasedmodelrouting.go): all four locations are
            # accepted, pathParam included, via rewritePathParameter -- a real
            # implementation, not just accepted-and-ignored.
            "supportedRequestModelLocations": {"payload", "header", "queryParam", "pathParam"},
            "operationPolicies": [{"name": "time-based-model-routing", "version": "v0", "paths": [{
                "path": path, "methods": ["POST"], "params": {
                    "timezone": "Europe/London",
                    "schedules": [{"from": "18:00", "to": "22:00", "model": {"modelName": small}}],
                    "fallback": {"modelName": large},
                    "requestModel": request_model,
                }}]}],
        },
        "cost-based-routing": {
            "context_suffix": "cost-routing", "displayName": "Cost-Based Routing", "wired": True,
            # Confirmed in ai-gateway/policies/cost-based-model-routing
            # (config.go: parseRequestModel): all four locations are
            # accepted, pathParam included -- same as time-based-model-routing.
            "supportedRequestModelLocations": {"payload", "header", "queryParam", "pathParam"},
            # cost-based-model-routing charges a budget from the cost llm-cost reports
            # in SharedContext.Metadata — global llm-cost alone isn't enough, it must
            # also run at OPERATION scope on the SAME path, AFTER this policy in the
            # chain (array order), or every call logs "no usable LLM cost was
            # reported, budget not charged" and the budget never moves.
            "operationPolicies": [
                {"name": "cost-based-model-routing", "version": "v0", "paths": [{
                    "path": path, "methods": ["POST"], "params": {
                        "modelBudgets": [
                            {"model": {"modelName": large}, "budgetLimits": [{"amount": budget, "duration": BUDGET_DURATION}]},
                            {"model": {"modelName": small}, "budgetLimits": [{"amount": 1000, "duration": BUDGET_DURATION}]},
                        ],
                        "onExhausted": "fallback",
                        "fallback": {"modelName": small},
                        "requestModel": request_model,
                    }}]},
                {"name": "llm-cost", "version": "v1", "paths": [{
                    "path": path, "methods": ["POST"], "params": {}}]},
            ],
        },
        # model-round-robin has the documented requestModel-at-global-scope defect
        # too (README.md:207-210), so it's provisioned the same OPERATION-scoped
        # way as the other routers. The fault-injection step (model genuinely
        # 503s) needs scripts/python/mocks/llm_fault_proxy.py pointed at by the Anthropic
        # provider's upstream -- see scripts/failover_demo.sh inject/restore.
        "model-failover": {
            "context_suffix": "failover", "displayName": "Model Failover", "wired": True,
            # model-round-robin is a STOCK policy (ships in the base gateway
            # image, no source in this repo to check). payload (Anthropic/
            # Mistral) confirmed alternating for real. pathParam (Gemini)
            # tested directly against the gateway and ruled OUT: the chain
            # builds with no error, but the policy never actually rewrites
            # the URL -- four calls all stayed on gemini-2.5-flash (confirmed
            # via each response's modelVersion), unlike time/cost-based-
            # model-routing, which do rewrite pathParam for real.
            "supportedRequestModelLocations": {"payload"},
            "operationPolicies": [{"name": "model-round-robin", "version": "v1", "paths": [{
                "path": path, "methods": ["POST"], "params": {
                    "models": [{"model": small}, {"model": large}],
                    "suspendDuration": 30,
                }}]}],
        },
    }


def build_lanes(provider: dict) -> dict:
    """One provider's full lane set, keyed by gateway lane id (prefixed for
    every provider but the default one — see PROVIDERS' lane_prefix).

    Skips a lane tagged supportedRequestModelLocations when the provider's
    own requestModel.location isn't in that set. The four routing/failover
    policies aren't equally capable: confirmed in their Go source,
    semantic-model-routing hard-rejects anything but "payload"
    (semanticmodelrouting.go:579), while time-based-model-routing and
    cost-based-model-routing both implement real path/header/query
    rewriting for the other three locations (rewritePathParameter /
    compilePathModelExpression) -- so Gemini, whose route addresses the
    model via the URL path ("pathParam"), can run the latter two but not the
    former. Confirmed against a real deploy: a mismatched location fails the
    whole policy chain build ("invalid params: 'requestModel.location' must
    be 'payload'", gateway-runtime log) -- not a config problem, so no
    params combination fixes a lane whose location isn't in its supported
    set."""
    location = provider["requestModel"]["location"]
    lanes = {}
    for base_id, tpl in lane_templates(provider).items():
        supported = tpl.get("supportedRequestModelLocations")
        if supported is not None and location not in supported:
            continue
        lane_id = provider["lanePrefix"] + base_id
        config = None
        if tpl["wired"]:
            # Only the key -- ai-chatbot/server.py computes this same lane's
            # context on its own (/<providerId>-<contextSuffix>, or bare
            # /<providerId> when contextSuffix is null, from
            # ai-chatbot/showcases.json), so there is no context var left to
            # write here; see resolve_gateway()'s own comment on that.
            config = {"api_key_var": env_key(base_id, "API_KEY", provider["id"])}
            # Historically the one global fallback ai-chatbot falls back to
            # when a showcase has no key of its own — keep writing it only
            # for the default provider's plain-chat, exactly as before.
            if base_id == "plain-chat" and provider["id"] == DEFAULT_PROVIDER_ID:
                config["api_key_var"] = ["GATEWAY_API_KEY", config["api_key_var"]]
        lanes[lane_id] = {
            "id": lane_id,
            "base_id": base_id,
            "providerId": provider["id"],
            "context": f"/{provider['id']}-{tpl['context_suffix']}" if tpl["context_suffix"] else f"/{provider['id']}",
            "displayName": tpl["displayName"],
            "globalPolicies": tpl.get("globalPolicies", []),
            "operationPolicies": tpl.get("operationPolicies", []),
            "config": config,  # None => gateway-only, not wired into ai-chatbot
        }
    return lanes


PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}
ALL_LANES = {}
for _p in PROVIDERS:
    ALL_LANES.update(build_lanes(_p))
DEFAULT_LANE_IDS = [lid for lid, l in ALL_LANES.items() if l["config"] is not None]


# =============================================================================
# 3. Gateway registration
# =============================================================================
def write_gateway_env(values: dict):
    f = ROOT / "ai-gateway" / "api-platform.env"
    text = f.read_text()
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text, count=1)
        else:
            text += f"\n{key}={value}\n"
    f.write_text(text)


def ensure_gateway_registered(gateway_id: str, display_name: str) -> bool:
    """Creates the gateway in the control plane and registers a fresh
    controller token if the gateway isn't already known or doesn't already
    hold one, writing it into ai-gateway/api-platform.env with TLS
    skip-verify (our self-signed cert doesn't cover host.docker.internal).
    Returns True if a fresh token was written (the controller needs a
    restart to pick it up).

    Deliberately does NOT gate on the gateway's `isActive` flag: that's a
    heartbeat-derived flag that flaps independently of whether the
    controller's already-issued token still works — confirmed by a
    controller that had been serving real gateway traffic for minutes while
    still reporting `isActive: false`. Gating a re-mint on it burns through
    GATEWAY_TOKEN_LIMIT_REACHED on every idempotent re-run.
    """
    status, resp = api("GET", f"/gateways/{gateway_id}")
    gw_env = ROOT / "ai-gateway" / "api-platform.env"
    has_token = bool(re.search(r"^APIP_GW_CONTROLLER_CONTROLPLANE_TOKEN=.+$", gw_env.read_text(), re.MULTILINE))
    if status == 200 and has_token:
        return False

    if status != 200:
        api("POST", "/gateways", {
            "displayName": display_name, "id": gateway_id,
            "endpoints": [GATEWAY_ENDPOINT], "functionalityType": "ai",
            "description": "Local dev gateway (offset ports)", "version": "1.2",
        })

    status, resp = api("POST", f"/gateways/{gateway_id}/tokens", {})
    if status != 201:
        sys.exit(f"  failed to generate gateway registration token: {status} {resp}")
    token = resp["token"]

    write_gateway_env({
        "APIP_GW_CONTROLLER_CONTROLPLANE_HOST": f"host.docker.internal:{DEMO_ENV['PLATFORM_API_PORT']}",
        "APIP_GW_CONTROLLER_CONTROLPLANE_TOKEN": token,
        "APIP_GW_CONTROLLER_CONTROLPLANE_INSECURE_SKIP_VERIFY": "true",
    })
    return True


def restart_gateway_controller():
    gw_dir = ROOT / "ai-gateway"
    subprocess.run(
        ["docker", "compose", "up", "-d", "--force-recreate", "gateway-controller"],
        cwd=str(gw_dir), check=True,
    )


def wait_for_gateway_active(gateway_id: str, timeout_s: int = 300):
    for _ in range(timeout_s // 2):
        time.sleep(2)
        status, resp = api("GET", f"/gateways/{gateway_id}")
        if status == 200 and resp.get("isActive"):
            print("  gateway active")
            return
    sys.exit(f"  gateway {gateway_id} did not become active within {timeout_s}s — "
              f"check: docker compose -f ai-gateway/docker-compose.yaml logs gateway-controller")


def ensure_gateway_name(gateway_id: str, display_name: str):
    status, resp = api("GET", f"/gateways/{gateway_id}")
    if status != 200:
        return
    if resp.get("displayName") == display_name:
        return
    api("PUT", f"/gateways/{gateway_id}", {
        "displayName": display_name, "endpoints": resp.get("endpoints"),
        "functionalityType": resp.get("functionalityType"),
        "description": resp.get("description"), "version": "1.2"})


# =============================================================================
# 4. Application / project grouping — bundles the shared provider and every
#    proxy under one GenAI Application so the ai-workspace UI shows one card
#    instead of a flat list of providers.
# =============================================================================
def ensure_project(project_id: str, display_name: str):
    status, _ = api("GET", f"/projects/{project_id}")
    if status == 200:
        return
    status, resp = api("POST", "/projects", {"id": project_id, "displayName": display_name})
    if status != 201:
        sys.exit(f"  failed to create project {project_id}: {status} {resp}")


def ensure_app(app_id: str, display_name: str, description: str):
    ensure_project(PROJECT_ID, "Default")
    status, _ = api("GET", f"/applications/{app_id}")
    payload = {"id": app_id, "displayName": display_name, "description": description,
               "projectId": PROJECT_ID, "type": "genai"}
    if status == 200:
        status, resp = api("PUT", f"/applications/{app_id}", payload)
    else:
        status, resp = api("POST", "/applications", payload)
        # The existence-check GET above has no retry, unlike deploy()/
        # login() — under host contention it can transiently fail (non-200
        # for a reason other than "doesn't exist"), making this wrongly
        # attempt a POST on an app that's already there. Fall back to PUT
        # instead of a hard failure once we know that's actually what happened.
        if status == 409 and str(resp.get("code", "")) == "APPLICATION_EXISTS":
            status, resp = api("PUT", f"/applications/{app_id}", payload)
    if status not in (200, 201):
        sys.exit(f"  failed to create/update application {app_id}: {status} {resp}")


def ensure_association(app_id: str, entity_id: str, kind: str):
    status, resp = api("GET", f"/applications/{app_id}/associations")
    existing = {a["id"] for a in resp.get("list", [])} if status == 200 else set()
    if entity_id in existing:
        return
    status, resp = api("POST", f"/applications/{app_id}/associations",
                        {"associations": [{"id": entity_id, "kind": kind}]})
    if status != 200:
        print(f"  WARNING: could not associate {entity_id} with {app_id}: {resp}")


def ensure_app_api_key(app_id: str, entity_id: str, kind: str, key_id: str):
    """Links an already-issued api-key-auth key (provider or proxy) to the
    GenAI Application, so it counts toward x-wso2-application-id (used by
    e.g. llm-cost-based-ratelimit's consumerBased option and app-level
    analytics). Payload confirmed against
    docs/rest-apis/platform-api/applications.md: POST /applications/{id}/api-keys
    with a nested associatedEntity object."""
    status, resp = api("GET", f"/applications/{app_id}/api-keys")
    existing = {k["keyId"] for k in resp.get("list", [])} if status == 200 else set()
    if key_id in existing:
        return
    status, resp = api("POST", f"/applications/{app_id}/api-keys",
                        {"apiKeys": [{"keyId": key_id,
                                      "associatedEntity": {"id": entity_id, "kind": kind}}]})
    if status != 200:
        print(f"  WARNING: could not link api-key {key_id} to {app_id}: {resp}")


# =============================================================================
# 5. Proxy lifecycle -- the per-showcase-lane half (provider lifecycle is
#    platform_api.py's upsert_shared_provider; see module docstring above).
#    Schemas confirmed against
#    docs/rest-apis/platform-api/{llm-providers,llm-proxies}.md.
# =============================================================================
def upsert_proxy(provider: dict, lane: dict):
    payload = {
        "id": lane["id"],
        "displayName": f"{provider['displayName']} - {lane['displayName']}",
        "description": f"{lane['displayName']} lane",
        "version": "v1",
        "projectId": PROJECT_ID,
        "context": lane["context"],
        "provider": {"id": provider["providerId"]},
        "globalPolicies": lane.get("globalPolicies", []),
        "operationPolicies": lane.get("operationPolicies", []),
        "policies": [],
        "openapi": "",
        "security": {"enabled": True, "apiKey": {"enabled": True, "key": "X-API-Key", "in": "header"}},
    }
    status, _ = api("GET", f"/llm-proxies/{lane['id']}")
    if status == 200:
        status, resp = api("PUT", f"/llm-proxies/{lane['id']}", payload)
        verb = "updated"
    else:
        status, resp = api("POST", "/llm-proxies", payload)
        verb = "created"
    if status not in (200, 201):
        sys.exit(f"  proxy {verb} failed: {status} {resp}")
    print(f"  proxy {verb}")


def issue_key(lane_id: str) -> tuple[str, str]:
    """Every run mints a key under a fresh, never-reused name, then cleans up
    this lane's older keys. Reusing a fixed name across runs (the previous
    approach: POST, and on conflict DELETE-then-POST the same name) races
    against the controller's async create/revoke event processing — under
    host contention that queue can deliver a stale event after a newer one,
    and the controller logs it plainly: "API key with name ... already
    exists ... with a different ID", followed by "API key not found in
    database for upsert event". The runtime then keeps serving whatever key
    it last durably stored, which may not be the one we just handed out —
    a silent, persistent 401 with no further retry able to fix it."""
    name = f"{lane_id}-key-{int(time.time() * 1000)}"
    status, resp = api("POST", f"/llm-proxies/{lane_id}/api-keys", {"displayName": name})
    if status != 201:
        sys.exit(f"  api-key creation failed: {status} {resp}")
    key_id, key_value = resp["id"], resp["apiKey"]
    status, listing = api("GET", f"/llm-proxies/{lane_id}/api-keys")
    if status == 200:
        for k in listing.get("list", []):
            if k["id"] != key_id:
                api("DELETE", f"/llm-proxies/{lane_id}/api-keys/{k['id']}")
    return key_id, key_value


def wait_for_key_live(context: str, messages_suffix: str, key: str, timeout_s: int = 180) -> bool:
    """A freshly issued key travels to the runtime over its own APIKeyState
    xDS channel, separate from the PolicyChainConfig one route_is_live()
    checks — the two can and do desync (observed: policy chain resynced
    fine after a controller reconnect while API key state lagged behind,
    leaving a just-issued key rejected for a while). Confirms this specific
    key is actually accepted before we hand it to ai-chatbot."""
    for _ in range(timeout_s // 2):
        status, _ = request("POST", f"https://{GATEWAY_ENDPOINT}{context}{messages_suffix}",
                             {"model": "probe", "max_tokens": 1, "messages": []},
                             {"X-API-Key": key})
        if status != 401:
            return True
        time.sleep(2)
    return False


# =============================================================================
# 6. ai-chatbot integration
# =============================================================================
def write_config_env(var: str, value: str):
    """Substitutes an existing VAR=... line in the gitignored
    GATEWAY_SECRETS_FILE, or appends one. Appending is what lets a brand new
    provider (Mistral's SHOWCASE_*_MISTRAL_API_KEY vars) show up with zero
    manual pre-authoring — the whole point of "add a provider to PROVIDERS
    and nothing else" (server.py's load_config() folds in any SHOWCASE_* key
    from the file, so nothing else needs to know these lines exist in
    advance). Every var this is ever called with is a real credential (an
    api-key-auth key) — the matching context each key pairs with is never
    written anywhere: ai-chatbot/server.py computes it on its own from
    ai-chatbot/showcases.json, see resolve_gateway()'s own comment there."""
    f = GATEWAY_SECRETS_FILE
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# Gateway-issued per-lane api-key-auth keys — generated, gitignored, never committed.\n"
                      "# Regenerate with ./scripts/python/provision_showcases.py.\n")
    text = f.read_text()
    pattern = re.compile(rf"^{re.escape(var)}=.*$", re.MULTILINE)
    if pattern.search(text):
        f.write_text(pattern.sub(f"{var}={value}", text, count=1))
    else:
        if not text.endswith("\n"):
            text += "\n"
        f.write_text(text + f"{var}={value}\n")


def restart_ai_chatbot():
    """Stopping goes through scripts/stop-ai-chatbot.sh — the one place that
    asks ai-chatbot to exit over its own loopback HTTP, instead of
    reimplementing that here a second time. No lsof, no pidfile: a pidfile
    goes stale the moment something else kills that process, the machine
    sleeps, or a previous run's file is just lost, and every later "restart"
    then spawns a new process that immediately dies on "Address already in
    use", silently leaving the untracked old one — with whatever stale keys
    it loaded at its own long-ago startup — as the one actually answering the
    browser. Confirmed exactly this: a server.py from months earlier was
    still the sole process listening on the port while newer spawns had
    already died. stop-ai-chatbot.sh itself already polls until the port
    stops answering (or exits non-zero if it doesn't), so check=True here is
    enough -- nothing to re-verify."""
    subprocess.run([str(ROOT / "scripts" / "stop-ai-chatbot.sh")], check=True)
    log = open(AI_CHATBOT_LOG_FILE, "w")
    proc = subprocess.Popen(["python3", "server.py"], cwd=str(ROOT / "ai-chatbot"),
                             stdout=log, stderr=log, start_new_session=True)
    time.sleep(2)
    if proc.poll() is not None:
        sys.exit(f"  ai-chatbot exited immediately after restart (code {proc.returncode}) — see {AI_CHATBOT_LOG_FILE}")


# =============================================================================
# 7. main
# =============================================================================
def main():
    requested = sys.argv[1:] or DEFAULT_LANE_IDS
    unknown = [r for r in requested if r not in ALL_LANES]
    if unknown:
        sys.exit(f"unknown lane(s): {unknown}. Known: {list(ALL_LANES.keys())}")

    # Only the providers actually needed by what was requested — running
    # `./provision_showcases.py spend-limit` shouldn't require every other
    # provider's key to be set in secrets/api-keys.env.
    needed_provider_ids = {ALL_LANES[lid]["providerId"] for lid in requested}
    providers = [p for p in PROVIDERS if p["id"] in needed_provider_ids]
    secrets = {p["id"]: read_secret(p) for p in providers}

    login()

    print("### gateway registration")
    if ensure_gateway_registered(GATEWAY_ID, GATEWAY_DISPLAY_NAME):
        print("  new token issued — recreating gateway-controller")
        restart_gateway_controller()
        wait_for_gateway_active(GATEWAY_ID)
    else:
        print("  already registered and active")
    ensure_gateway_name(GATEWAY_ID, GATEWAY_DISPLAY_NAME)

    for provider in providers:
        pid = provider["id"]
        print(f"### {provider['displayName']} application + provider")
        ensure_app(provider["appId"], provider["appDisplayName"], provider["appDescription"])

        upsert_shared_provider(provider, secrets[pid])
        deploy(provider["providerId"], provider["providerContext"], provider["messagesSuffix"],
               resource="llm-providers")
        ensure_association(provider["appId"], provider["providerId"], "LlmProvider")

        for lane_id in requested:
            lane = ALL_LANES[lane_id]
            if lane["providerId"] != pid:
                continue
            print(f"### {lane_id} ({lane['context']})")
            upsert_proxy(provider, lane)
            deploy(lane_id, lane["context"], provider["messagesSuffix"])
            ensure_association(provider["appId"], lane_id, "LlmProxy")
            key_id, key = issue_key(lane_id)
            ensure_app_api_key(provider["appId"], lane_id, "LlmProxy", key_id)
            if not wait_for_key_live(lane["context"], provider["messagesSuffix"], key):
                sys.exit(f"  key issued but never accepted by the gateway within 180s — "
                          f"check gateway-controller logs for delayed apikey.created processing "
                          f"(a CPU-starved host can delay this by minutes)")
            print("  key confirmed live on the gateway")

            config = lane["config"]
            if config:
                key_vars = config["api_key_var"]
                for var in ([key_vars] if isinstance(key_vars, str) else key_vars):
                    write_config_env(var, key)
                print(f"  {key_vars} written to secrets/gateway-keys.env")
            else:
                print(f"  api key: {key}  (no showcases.json entry — not wired into ai-chatbot)")

    restart_ai_chatbot()
    apps = ", ".join(f"'{p['appId']}'" for p in providers)
    print(f"\ndone. Lanes grouped under GenAI Application(s) {apps}. "
          "ai-chatbot restarted with the new keys.")


if __name__ == "__main__":
    main()
