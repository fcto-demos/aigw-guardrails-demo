#!/usr/bin/env python3
"""Transparent pass-through proxy in front of a real LLM provider API, built
for exactly one purpose: the model-failover showcase needs a real 5xx from a
named model on demand, and no provider will produce one to order.

Every request is forwarded to the real upstream untouched -- same headers
(the gateway's real api key included), same body, response streamed back
chunk for chunk -- EXCEPT when the request names the model currently marked
"broken" (see /__control__/break), in which case this returns a realistic
error for that provider at 503 and never calls upstream at all. Answers from
every other model are real answers from the real provider.

One instance per provider (see PROVIDER_DEFAULTS), each sitting permanently
in that provider's LlmProvider upstream slot (see scripts/failover_demo.sh
inject/restore) -- normal operation is 100% passthrough, so every other
showcase on that provider is unaffected.

Stdlib only, no deps -- same convention as ai-chatbot/server.py.
"""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# modelLocation matches providers.json's own requestModel.location for this
# provider: "payload" means the model id is $.model in the JSON body
# (Anthropic and Mistral's OpenAI-compatible shape both use that top-level
# field); "pathParam" means it's embedded in the URL, Gemini's
# /v1beta/models/<id>:generateContent.
PROVIDER_DEFAULTS = {
    "anthropic": {
        "upstream": "https://api.anthropic.com",
        "errorBody": {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        "modelLocation": "payload",
    },
    "mistral": {
        "upstream": "https://api.mistral.ai",
        "errorBody": {"message": "Service unavailable.", "type": "model_overloaded", "code": 1900},
        "modelLocation": "payload",
    },
    "gemini": {
        "upstream": "https://generativelanguage.googleapis.com",
        "errorBody": {"error": {"code": 503, "message": "The model is overloaded. Please try again later.",
                                 "status": "UNAVAILABLE"}},
        "modelLocation": "pathParam",
    },
}

_PATH_MODEL_RE = re.compile(r"models/([A-Za-z0-9.-]+)")

# Default port per provider -- scripts/failover_demo.sh and
# provision_showcases.py's FAULT_PROXY_PORTS must agree with these.
DEFAULT_PORTS = {"anthropic": 8602, "mistral": 8603, "gemini": 8604}

# Hop-by-hop headers that must never be forwarded verbatim in either
# direction -- the connection here is not the same connection upstream.
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "host"}

_lock = threading.Lock()
_broken_model = None
# Set once in main() from --provider/--upstream; never reassigned per request.
_upstream = None
_error_body = None
_model_location = None


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: the socket closing IS the end-of-body signal, so a chunked or
    # unsized upstream response can be relayed byte-for-byte with no framing
    # of our own to get right. This is a local, one-shot demo tool -- no
    # keep-alive performance to chase.
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        print("[fault-proxy] %s" % (fmt % args))

    def _control(self):
        global _broken_model
        if self.path == "/__control__/status":
            with _lock:
                body = json.dumps({"brokenModel": _broken_model}).encode()
            return self._reply(200, body)
        if self.path == "/__control__/break" and self.command == "POST":
            payload = self._read_json()
            model = (payload or {}).get("model")
            if not model:
                return self._reply(400, b'{"error":"body must be {\\"model\\": \\"...\\"}"}')
            with _lock:
                _broken_model = model
            print("[fault-proxy] now failing every request for %r" % model)
            return self._reply(200, json.dumps({"brokenModel": model}).encode())
        if self.path == "/__control__/heal" and self.command == "POST":
            with _lock:
                _broken_model = None
            print("[fault-proxy] healed -- full passthrough restored")
            return self._reply(200, b'{"brokenModel": null}')
        return self._reply(404, b'{"error":"unknown control path"}')

    def _read_json(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def _reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        if self.path.startswith("/__control__/"):
            return self._control()

        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        if _model_location == "pathParam":
            match = _PATH_MODEL_RE.search(self.path)
            model = match.group(1) if match else None
        elif body:
            try:
                model = json.loads(body).get("model")
            except (json.JSONDecodeError, ValueError):
                model = None
        else:
            model = None

        if model:
            with _lock:
                broken = _broken_model
            if model == broken:
                print("[fault-proxy] injecting 503 for %r (upstream NOT called)" % model)
                return self._reply(503, _error_body)

        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        request = Request(_upstream + self.path, data=body or None, headers=headers, method=self.command)
        try:
            with urlopen(request, timeout=120) as upstream:
                self._relay(upstream.status, upstream.headers, upstream)
        except HTTPError as exc:
            self._relay(exc.code, exc.headers, exc)
        except URLError as exc:
            self._reply(502, json.dumps({"error": "fault-proxy: upstream unreachable: %s" % exc}).encode())

    def _relay(self, status, headers, fileobj):
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in _HOP_BY_HOP:
                self.send_header(key, value)
        self.end_headers()
        while True:
            chunk = fileobj.read(8192)
            if not chunk:
                break
            self.wfile.write(chunk)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def main():
    global _upstream, _error_body, _model_location
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=sorted(PROVIDER_DEFAULTS))
    parser.add_argument("--port", type=int, default=None,
                         help="defaults to this provider's entry in DEFAULT_PORTS")
    parser.add_argument("--upstream", default=None, help="override the real upstream URL")
    args = parser.parse_args()

    defaults = PROVIDER_DEFAULTS[args.provider]
    _upstream = (args.upstream or defaults["upstream"]).rstrip("/")
    _error_body = json.dumps(defaults["errorBody"]).encode()
    _model_location = defaults["modelLocation"]
    port = args.port or DEFAULT_PORTS[args.provider]

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("fault-proxy[%s] listening on :%d -> %s (passthrough until /__control__/break)"
          % (args.provider, port, _upstream))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
