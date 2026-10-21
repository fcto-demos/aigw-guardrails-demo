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


# Any of these set means a remote target; the first three are then required.
REMOTE_SETTINGS = ("REMOTE_AIW_URL", "REMOTE_GATEWAY_ID", "REMOTE_GATEWAY_URL", "REMOTE_ADMIN_USERNAME",
                   "REMOTE_PROJECT_ID", "REMOTE_GATEWAY_CONTROLLER_URL", "REMOTE_MCP_GATEWAY_ID",
                   "REMOTE_MCP_GATEWAY_URL")
REMOTE = any(setting(name) for name in REMOTE_SETTINGS)
if REMOTE and not all(setting(name) for name in REMOTE_SETTINGS[:3]):
    sys.exit(f"a remote target needs {', '.join(REMOTE_SETTINGS[:3])} -- see demo.env.example")

# id: the gateway's id in AI Workspace. url: where its proxies answer.
# controller_url: its management API, which says what the gateway holds
# without sending traffic through it, so the checks leave nothing in
# analytics. Without one, the checks call the gateway's routes instead.
Gateway = namedtuple("Gateway", "id url controller_url")

_ports = gateway_ports()
_controller = setting("REMOTE_GATEWAY_CONTROLLER_URL") if REMOTE \
    else f"http://localhost:{_ports['GATEWAY_CONTROLLER_REST_PORT']}"

WORKSPACE_URL = setting("REMOTE_AIW_URL", f"https://localhost:{DEMO_ENV['AI_WORKSPACE_PORT']}").rstrip("/")
PROJECT_ID = setting("REMOTE_PROJECT_ID", "default")
ADMIN_USERNAME = setting("REMOTE_ADMIN_USERNAME", "admin")
# From the environment only: demo.env holds no secret. This machine's is the
# one scripts/generate-local-certs.sh hashed.
ADMIN_PASSWORD = os.environ.get("REMOTE_ADMIN_PASSWORD", "") if REMOTE else os.environ.get("ADMIN_PASSWORD", "admin")

LLM_GATEWAY = Gateway(setting("REMOTE_GATEWAY_ID", "local-gateway"),
                      setting("REMOTE_GATEWAY_URL", f"https://localhost:{_ports['GATEWAY_HTTPS_PORT']}").rstrip("/"),
                      _controller.rstrip("/"))
# The MCP proxies' gateway: the same one unless REMOTE_MCP_GATEWAY_* names
# another, whose controller is then not known.
_mcp_id = setting("REMOTE_MCP_GATEWAY_ID", LLM_GATEWAY.id)
MCP_GATEWAY = Gateway(_mcp_id, setting("REMOTE_MCP_GATEWAY_URL", LLM_GATEWAY.url).rstrip("/"),
                      LLM_GATEWAY.controller_url if _mcp_id == LLM_GATEWAY.id else "")
