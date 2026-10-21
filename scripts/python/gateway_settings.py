"""Which cards the gateway can run. Some cards need a gateway setting: an
identity provider's URLs, or a content safety service's key. While it is
blank, the provisioning skips the card and the chatbot shows it as not wired.

For the identity provider, config.toml says which settings a key manager uses:

    [[policy_configurations.jwtauth_v1.keymanagers]]
    name   = "mcp-idp"
    issuer = '{{ env "IDP_ISSUER" "" }}'

so a card whose policy names "mcp-idp" needs IDP_ISSUER (and IDP_JWKS_URI).
"""
import re
import tomllib

from target import ROOT, read_env_file

# What the policies calling an outside service need.
SETTINGS_READ_BY_POLICY = {
    "azure-content-safety-content-moderation": ["AZURE_CONTENT_SAFETY_ENDPOINT", "AZURE_CONTENT_SAFETY_KEY"],
    "aws-bedrock-guardrail": ["AWS_BEDROCK_REGION", "AWS_BEDROCK_GUARDRAIL_ID", "AWS_BEDROCK_GUARDRAIL_VERSION"],
    "typesafe-jev-content-safety": ["TYPESAFE_API_KEY"],
    "typesafe-jev-mcp-tool-result-screening": ["TYPESAFE_API_KEY"],
    "typesafe-jev-model-routing": ["TYPESAFE_API_KEY"],
}

# The files the gateway reads its settings from (ai-gateway/docker-compose.yaml).
GATEWAY_ENV_FILES = ["ai-gateway/api-platform.env", "demo.env", "secrets/idp.env", "secrets/content-safety.env"]


def key_managers() -> dict:
    """config.toml's key managers, with the settings each one uses."""
    config = tomllib.loads((ROOT / "ai-gateway" / "configs" / "config.toml").read_text())
    return {manager["name"]: re.findall(r'\{\{\s*env\s+"(\w+)"', str(manager))
            for manager in config["policy_configurations"]["jwtauth_v1"]["keymanagers"]}


def missing_gateway_settings(card: dict) -> str:
    """What a card still needs set on the gateway, "" if nothing. A blank
    value, or the example's <placeholder>, counts as missing."""
    policies = ((card.get("mcp") or {}).get("policies") or []) + \
        [{"name": p.split()[0]} for p in card.get("policies") or []]
    needed = []
    for policy in policies:
        params = str(policy.get("params") or {})
        needed += [v for name, variables in key_managers().items() if f"'{name}'" in params for v in variables]
        needed += SETTINGS_READ_BY_POLICY.get(policy["name"], [])

    env = {}
    for file in GATEWAY_ENV_FILES:
        if (ROOT / file).exists():
            env.update(read_env_file(ROOT / file))
    missing = [v for v in dict.fromkeys(needed) if not env.get(v) or "<" in env[v]]
    return f"{', '.join(missing)} set in secrets/ (see the .example files there)" if missing else ""
