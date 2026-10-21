"""The gateway settings a card's policies rely on, and which of them this
repo's gateway still lacks. The provisioning leaves such a card out and the
chatbot shows it unwired with the reason, both through
missing_gateway_settings(), so the two always agree.

ai-gateway/configs/config.toml is the reference: a value it reads from an
environment variable with a blank default ({{ env "NAME" "" }}) is a setting
the gateway cannot do without. A policy relies on a setting in two ways:
  - its params name an entry of config.toml, such as the key manager
    "mcp-idp", whose issuer and JWKS URL come from the identity provider;
  - it reads config.toml values on its own (POLICY_CONFIG_KEYS below).
"""
import re
import tomllib

from target import ROOT, read_env_file

CONFIG_TOML = ROOT / "ai-gateway" / "configs" / "config.toml"

# The files the gateway's containers take their environment from: the
# env_file entries of ai-gateway/docker-compose.yaml.
GATEWAY_ENV_FILES = ("ai-gateway/api-platform.env", "demo.env", "secrets/idp.env", "secrets/content-safety.env")

# The config.toml values a policy reads by itself, whatever its params say:
# the ${config.<key>} defaults in its definition.
POLICY_CONFIG_KEYS = {
    "azure-content-safety-content-moderation": ("azurecontentsafety_endpoint", "azurecontentsafety_key"),
    "aws-bedrock-guardrail": ("awsbedrock_guardrail_region", "awsbedrock_guardrail_id",
                              "awsbedrock_guardrail_version"),
}

REQUIRED_ENV = re.compile(r'\{\{\s*env\s+"(\w+)"\s+""\s*\}\}')


def _values(node):
    """Every scalar under a TOML table or a JSON document, nested ones included."""
    if isinstance(node, dict):
        return [v for child in node.values() for v in _values(child)]
    if isinstance(node, list):
        return [v for child in node for v in _values(child)]
    return [node]


def _named_entries(node) -> dict:
    """The config.toml entries listed under a name, such as the key
    managers: name -> entry."""
    if isinstance(node, dict):
        return {k: v for child in node.values() for k, v in _named_entries(child).items()}
    if not isinstance(node, list):
        return {}
    found = {child["name"]: child for child in node if isinstance(child, dict) and isinstance(child.get("name"), str)}
    for child in node:
        found.update(_named_entries(child))
    return found


def _required_env(node) -> list:
    return [name for value in _values(node) if isinstance(value, str) for name in REQUIRED_ENV.findall(value)]


def needed_settings(policies: list) -> list:
    """The environment variables the gateway needs set for these policies
    ({"name": ..., "params": ...}, as attached to a proxy) to work."""
    config = tomllib.loads(CONFIG_TOML.read_text())
    named = _named_entries(config)
    names = []
    for policy in policies:
        names += [n for value in _values(policy) if isinstance(value, str) and value in named
                  for n in _required_env(named[value])]
        names += [n for key in POLICY_CONFIG_KEYS.get(policy["name"], ()) for n in _required_env(config.get(key))]
    return list(dict.fromkeys(names))


def _where_to_set(names: list) -> str:
    """'IDP_ISSUER, IDP_JWKS_URI in secrets/idp.env': each setting with the
    file whose example lists it."""
    by_file = {}
    for name in names:
        file = next((f for f in GATEWAY_ENV_FILES
                     if (ROOT / f"{f}.example").exists() and name in read_env_file(ROOT / f"{f}.example")),
                    "the gateway's environment")
        by_file.setdefault(file, []).append(name)
    return "; ".join(f"{', '.join(found)} in {file}" for file, found in by_file.items())


def missing_gateway_settings(card: dict) -> str:
    """What a showcases.json card still needs set for the gateway to run its
    policies ("" when nothing): its MCP proxy's policies, with their params,
    and the ones its "policies" list names ("aws-bedrock-guardrail v1.2.1").
    A setting counts as missing when the gateway's env files leave it blank
    or still hold the example's <placeholder>."""
    policies = ((card.get("mcp") or {}).get("policies") or []) + \
        [{"name": p.split()[0]} for p in card.get("policies") or []]
    env = {}
    for name in GATEWAY_ENV_FILES:
        if (ROOT / name).exists():
            env.update(read_env_file(ROOT / name))
    missing = [n for n in needed_settings(policies) if not env.get(n) or "<" in env[n]]
    return _where_to_set(missing) if missing else ""
