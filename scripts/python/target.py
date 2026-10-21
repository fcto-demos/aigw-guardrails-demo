"""Where the demo works, in one place: the AI Workspace the provisioning
writes to, and the gateways its proxies deploy to and the chatbot calls.

This machine's own stack by default. The REMOTE_ settings, in demo.env or
the environment, name an AI Workspace and gateways already running elsewhere
instead (see demo.env.example). Read by the provisioning and the chatbot
alike, so an address changes here and nowhere else. Import only.
"""
import os
import sys
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def read_env_file(path: Path) -> dict:
    """KEY=VALUE lines, as demo.env and the secrets/ files hold them."""
    conf = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip()
    return conf


def _read_demo_env() -> dict:
    """This machine's settings from demo.env. On the first run, demo.env is
    created from demo.env.example, as the shell scripts do."""
    path, example = ROOT / "demo.env", ROOT / "demo.env.example"
    if not path.exists():
        if not example.exists():
            sys.exit(f"missing {example} -- run this from a checkout of the repo")
        path.write_text(example.read_text())
        print(f"  created {path.name} from {example.name} -- adapt it to this machine if needed")
    return read_env_file(path)


DEMO_ENV = _read_demo_env()


def setting(name: str, default: str = "") -> str:
    """A setting from the environment, else from demo.env, else the default."""
    return os.environ.get(name) or DEMO_ENV.get(name) or default


def gateway_ports() -> dict:
    """This machine's gateway ports, each moved up by GATEWAY_PORT_OFFSET."""
    offset = int(DEMO_ENV.get("GATEWAY_PORT_OFFSET") or 0)
    return {key: str(int(value) + offset)
            for key, value in read_env_file(ROOT / "ai-gateway" / ".env").items()
            if key.startswith("GATEWAY_") and key.endswith("_PORT") and value.isdigit()}


# id: the gateway's id in AI Workspace. url: where its proxies answer.
# controller_url: its management API, which says what the gateway holds
# without sending traffic through it, so the checks leave nothing in
# analytics; without one, they call the gateway's routes. local: this
# machine's own gateway, which the provisioning registers.
Gateway = namedtuple("Gateway", "id url controller_url local")


def remote_gateway(prefix: str):
    """The gateway <prefix>_ID and <prefix>_URL name (both, or neither),
    with <prefix>_CONTROLLER_URL if set. None when they name none."""
    gateway_id, url = setting(f"{prefix}_ID"), setting(f"{prefix}_URL")
    if bool(gateway_id) != bool(url):
        sys.exit(f"{prefix}_ID and {prefix}_URL go together -- see demo.env.example")
    return Gateway(gateway_id, url.rstrip("/"), setting(f"{prefix}_CONTROLLER_URL").rstrip("/"), False) \
        if gateway_id else None


# This machine's stack, unless REMOTE_AIW_URL names an AI Workspace already
# running elsewhere, with the gateway REMOTE_GATEWAY_ID and _URL name there.
REMOTE = bool(setting("REMOTE_AIW_URL"))
_remote_llm = remote_gateway("REMOTE_GATEWAY")
if REMOTE != bool(_remote_llm):
    sys.exit("REMOTE_AIW_URL, REMOTE_GATEWAY_ID and REMOTE_GATEWAY_URL go together -- see demo.env.example")

if REMOTE:
    WORKSPACE_URL = setting("REMOTE_AIW_URL").rstrip("/")
    PROJECT_ID = setting("REMOTE_PROJECT_ID", "default")
    ADMIN_USERNAME = setting("REMOTE_ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("REMOTE_ADMIN_PASSWORD", "")  # never in demo.env
    LLM_GATEWAY = _remote_llm
else:
    _ports = gateway_ports()
    WORKSPACE_URL = f"https://localhost:{DEMO_ENV['AI_WORKSPACE_PORT']}"
    PROJECT_ID = "default"
    ADMIN_USERNAME = "admin"
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")  # the one generate-local-certs.sh hashed
    LLM_GATEWAY = Gateway("local-gateway", f"https://localhost:{_ports['GATEWAY_HTTPS_PORT']}",
                          f"http://localhost:{_ports['GATEWAY_CONTROLLER_REST_PORT']}", True)

# The MCP proxies go to another gateway when REMOTE_MCP_GATEWAY_ID and _URL
# name one, with this machine's stack as with a remote one.
MCP_GATEWAY = remote_gateway("REMOTE_MCP_GATEWAY") or LLM_GATEWAY
