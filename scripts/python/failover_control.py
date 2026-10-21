#!/usr/bin/env python3
"""Drives model-failover's live fault-injection step -- a different concern
from provision_showcases.py's one-time provisioning, though it reuses the
same Platform API client (platform_api.py): this toggles a provider's
upstream back and forth, repeatedly, during a live demo, while provisioning
brings an environment up once (idempotently) and is done.

Usage (see scripts/failover_demo.sh for the usual entrypoint):
    ./scripts/python/failover_control.py inject [provider]   # point it at the fault-injection mock
    ./scripts/python/failover_control.py break [provider]    # mock now fails that provider's large model
    ./scripts/python/failover_control.py heal [provider]     # mock back to passthrough
    ./scripts/python/failover_control.py restore [provider]  # point it back at the real API
    # provider defaults to anthropic; must have a mock entry in FAULT_PROXY_PORTS
    # and in scripts/python/mocks/llm_fault_proxy.py's own PROVIDER_DEFAULTS.
"""
import sys

from platform_api import PROVIDERS, read_secret, login, upsert_shared_provider, deploy, request
from target import LLM_GATEWAY

# Ports must agree with scripts/python/mocks/llm_fault_proxy.py's own DEFAULT_PORTS.
FAULT_PROXY_PORTS = {"anthropic": 8602, "mistral": 8603, "gemini": 8604}


def _fault_provider(provider_id: str) -> dict:
    if provider_id not in FAULT_PROXY_PORTS:
        sys.exit(f"no fault-injection mock for provider {provider_id!r} -- known: {sorted(FAULT_PROXY_PORTS)}")
    # FAULT_PROXY_PORTS and PROVIDERS (providers.json, filtered to enabled
    # ones) are two independently-maintained lists -- a provider named in one
    # but disabled (or removed) in the other needs its own clear error, not
    # next()'s bare StopIteration.
    provider = next((p for p in PROVIDERS if p["id"] == provider_id), None)
    if provider is None:
        sys.exit(f"{provider_id!r} has a fault-injection mock configured but isn't an enabled "
                  f"provider in providers.json -- known enabled providers: {[p['id'] for p in PROVIDERS]}")
    return provider


def _control(provider_id: str, path: str, data=None):
    """POST to the mock's own control endpoint, through platform_api's
    request() -- the same hardened client used for the Platform API itself,
    so a mock that isn't running fails with the same clean (0, {...}) this
    codebase handles everywhere else, not a raw urllib traceback."""
    url = f"http://localhost:{FAULT_PROXY_PORTS[provider_id]}/__control__/{path}"
    status, resp = request("POST", url, data if data is not None else {})
    if status != 200:
        sys.exit(f"  mock not reachable at {url} ({status}: {resp}) -- is it running? "
                  f"python3 scripts/python/mocks/llm_fault_proxy.py --provider {provider_id} "
                  f"--port {FAULT_PROXY_PORTS[provider_id]}")
    return resp


def inject(provider_id: str):
    provider = _fault_provider(provider_id)
    port = FAULT_PROXY_PORTS[provider_id]
    # Checked BEFORE touching the gateway: this upstream is shared by every
    # lane on this provider, not just model-failover, so pointing it at a
    # mock that isn't running orphans all of them until someone notices and
    # runs restore. Fail here instead, while nothing has changed yet.
    _control(provider_id, "status")
    secret = read_secret(provider)
    mock = dict(provider, upstreamUrl=f"http://host.docker.internal:{port}")
    login()
    upsert_shared_provider(mock, secret)
    deploy(LLM_GATEWAY, "llm-providers", mock["providerId"], mock["providerContext"], mock["messagesSuffix"])
    print(f"  {provider['displayName']} upstream now {mock['upstreamUrl']}")


def break_(provider_id: str):
    provider = _fault_provider(provider_id)
    model = provider["models"]["large"]
    resp = _control(provider_id, "break", {"model": model})
    print(f"  {resp}  (model: {model})")


def heal(provider_id: str):
    _fault_provider(provider_id)  # validates provider_id, result unused
    resp = _control(provider_id, "heal")
    print(f"  {resp}")


def restore(provider_id: str):
    provider = _fault_provider(provider_id)
    secret = read_secret(provider)
    login()
    upsert_shared_provider(provider, secret)
    deploy(LLM_GATEWAY, "llm-providers", provider["providerId"], provider["providerContext"],
           provider["messagesSuffix"])
    print(f"  {provider['displayName']} upstream restored to {provider['upstreamUrl']}")


MODES = {"inject": inject, "break": break_, "heal": heal, "restore": restore}


def main():
    if not sys.argv[1:] or sys.argv[1] not in MODES:
        sys.exit(f"usage: {sys.argv[0]} {{{'|'.join(MODES)}}} [provider=anthropic]")
    provider_id = sys.argv[2] if len(sys.argv) > 2 else "anthropic"
    MODES[sys.argv[1]](provider_id)


if __name__ == "__main__":
    main()
