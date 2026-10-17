#!/usr/bin/env python3
"""Acme Mobile's MCP server: the backend the MCP showcases put behind the
gateway. Fictional data (the plans match ai-chatbot/plans.md), one signed-in
demo customer, three tools.

Speaks MCP's Streamable HTTP transport in its simplest form: JSON-RPC over
POST /mcp, one JSON reply per request, no server-sent events. initialize
opens a session (the Mcp-Session-Id header) that every later request names.
Standard library only, so the stock python image runs it as-is (see
mcp-servers/docker-compose.yaml).
"""
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLANS = {
    "Lite": {"data": "5 GB", "price": "£6/month"},
    "Everyday": {"data": "15 GB", "price": "£9/month"},
    "Plus": {"data": "30 GB", "price": "£12/month"},
    "Max": {"data": "100 GB", "price": "£18/month"},
    "Unlimited": {"data": "Unlimited", "price": "£25/month"},
}

# The signed-in customer's account as each MCP session starts. change_plan
# changes it in that session only, so a client that opens a new session starts
# on Everyday again, whatever another session changed.
ACCOUNT = {"plan": "Everyday", "dataUsedThisMonth": "11.2 GB", "renewalDate": "2026-11-03"}
SESSIONS = {}  # Mcp-Session-Id -> the account in that session
MAX_SESSIONS = 1000

TOOLS = [
    {"name": "list_plans",
     "description": "List the SIM-only plans on sale, with their data allowance and monthly price.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_account_details",
     "description": "The signed-in customer's current plan, data used this month and renewal date.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "change_plan",
     "description": "Switch the signed-in customer to another plan. Takes effect immediately.",
     "inputSchema": {"type": "object",
                     "properties": {"plan": {"type": "string", "enum": list(PLANS),
                                             "description": "The plan to switch to."}},
                     "required": ["plan"]}},
]


def call_tool(name, args, account):
    """(result, is_error) for one tools/call."""
    if name == "list_plans":
        return [dict(plan=plan, **details) for plan, details in PLANS.items()], False
    if name == "get_account_details":
        return dict(account, allowance=PLANS[account["plan"]]["data"]), False
    if name == "change_plan":
        wanted = str(args.get("plan", "")).strip().lower()
        plan = next((p for p in PLANS if p.lower() == wanted), None)
        if plan is None:
            return {"error": "unknown plan %r, expected one of %s" % (args.get("plan"), list(PLANS))}, True
        previous, account["plan"] = account["plan"], plan
        return {"previousPlan": previous, "newPlan": plan, "price": PLANS[plan]["price"]}, False
    return {"error": "unknown tool %r" % name}, True


def handle(message, account):
    """The JSON-RPC reply to one request, in the session that owns account."""
    method, params, rid = message.get("method"), message.get("params") or {}, message.get("id")
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "acme-mobile", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        output, is_error = call_tool(params.get("name"), params.get("arguments") or {}, account)
        print("tools/call %s %s -> %s" % (params.get("name"), json.dumps(params.get("arguments") or {}),
                                         "error" if is_error else "ok"), flush=True)
        result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # tools/call lines above are the log worth reading

    def _reply(self, status, payload, session=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        if session:
            self.send_header("mcp-session-id", session)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/mcp":
            return self._reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("content-length") or 0)
            message = json.loads(self.rfile.read(max(length, 0)) or b"{}")
        except ValueError:
            return self._reply(400, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}})
        if not isinstance(message, dict):
            return self._reply(400, {"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32600, "message": "expected one JSON-RPC object"}})
        if message.get("method") == "initialize":
            if len(SESSIONS) >= MAX_SESSIONS:
                SESSIONS.clear()
            session = uuid.uuid4().hex
            SESSIONS[session] = dict(ACCOUNT)
            return self._reply(200, handle(message, SESSIONS[session]), session)
        # Every other request names its session, as the transport requires: a
        # missing one is a client error, an unknown one means initialize again.
        session = self.headers.get("mcp-session-id")
        if not session or session not in SESSIONS:
            return self._reply(400 if not session else 404, {
                "jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32000, "message": "no session: send initialize first"}})
        if "id" not in message:  # a notification: acknowledged, never answered
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        self._reply(200, handle(message, SESSIONS[session]))

    def do_GET(self):
        # No server-to-client stream: everything this server says is a reply.
        self._reply(405, {"error": "use POST /mcp"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print("acme-mobile MCP server listening on :%d/mcp" % port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
