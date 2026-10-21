"""Which cards this gateway can run. Some policies need a setting the gateway
only gets from its environment: an identity provider's URLs, a content
safety service's endpoint and key. While one is blank, the card is left out
of the provisioning and shown unwired in the chatbot. Both ask
missing_gateway_settings(), so they always agree.

The identity provider's URLs reach the gateway through a key manager in
ai-gateway/configs/config.toml, filled from environment variables:

    [[policy_configurations.jwtauth_v1.keymanagers]]
    name   = "mcp-idp"
    issuer = '{{ env "IDP_ISSUER" "" }}'
    ...jwks.remote: uri = '{{ env "IDP_JWKS_URI" "" }}'

A card whose policy names "mcp-idp" (mcp-auth's issuers) therefore needs
IDP_ISSUER and IDP_JWKS_URI. The content safety policies name nothing in
their params: they read their settings by themselves, listed below.
"""
import re
import tomllib

from target import ROOT, read_env_file

# What the content safety policies read from the gateway's environment.
SETTINGS_READ_BY_POLICY = {
    "azure-content-safety-content-moderation": ["AZURE_CONTENT_SAFETY_ENDPOINT", "AZURE_CONTENT_SAFETY_KEY"],
    "aws-bedrock-guardrail": ["AWS_BEDROCK_REGION", "AWS_BEDROCK_GUARDRAIL_ID", "AWS_BEDROCK_GUARDRAIL_VERSION"],
}

# The files the gateway's containers take their environment from: the
# env_file entries of ai-gateway/docker-compose.yaml.
GATEWAY_ENV_FILES = ["ai-gateway/api-platform.env", "demo.env", "secrets/idp.env", "secrets/content-safety.env"]


def key_managers() -> dict:
    """config.toml's key managers: name -> the environment variables they are filled from."""
    config = tomllib.loads((ROOT / "ai-gateway" / "configs" / "config.toml").read_text())
    return {manager["name"]: re.findall(r'\{\{\s*env\s+"(\w+)"', str(manager))
            for manager in config["policy_configurations"]["jwtauth_v1"]["keymanagers"]}


def missing_gateway_settings(card: dict) -> str:
    """What a showcases.json card still needs set on the gateway, "" when
    nothing. Its policies are its MCP proxy's, with their params, and the ones
    its "policies" list names ("aws-bedrock-guardrail v1.2.1"). A setting is
    missing when blank, or still the example's <placeholder>."""
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
