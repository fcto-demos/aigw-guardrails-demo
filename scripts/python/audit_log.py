#!/usr/bin/env python3
"""Turns the gateway runtime's log, read on stdin, into the audit record the
log-message policy keeps: each question and each answer, with its time and
lane. Used by scripts/audit-log.sh."""
import json
import re
import sys

LINE = re.compile(r'time=(\S+) .*?msg="(.*)"\s*$')


def question(payload: dict) -> str:
    """The last thing the customer said, whatever the wire format."""
    messages = payload.get("messages") or payload.get("contents") or []
    for message in reversed(messages):
        if message.get("role") in ("user", None):
            content = message.get("content")
            if isinstance(content, str):
                return content
            parts = message.get("parts") or content or []
            return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return ""


def answer(payload: dict) -> str:
    if payload.get("content"):                                    # Anthropic
        return " ".join(b.get("text", "") for b in payload["content"] if isinstance(b, dict))
    if payload.get("choices"):                                    # OpenAI format (Mistral)
        return (payload["choices"][0].get("message") or {}).get("content") or ""
    if payload.get("candidates"):                                 # Gemini
        parts = (payload["candidates"][0].get("content") or {}).get("parts") or []
        return " ".join(p.get("text", "") for p in parts)
    return json.dumps(payload)[:200]


def tokens(payload: dict) -> str:
    usage = payload.get("usage") or payload.get("usageMetadata") or {}
    used_in = usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("promptTokenCount")
    used_out = usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("candidatesTokenCount")
    return f"  [tokens in {used_in}, out {used_out}]" if used_in or used_out else ""


def short(text: str, width: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width] + "..."


count = 0
for line in sys.stdin:
    match = LINE.search(line)
    if not match or "mediation-flow" not in match.group(2):
        continue
    try:
        record = json.loads(json.loads('"' + match.group(2) + '"'))
        payload = json.loads(record["payload"]) if "payload" in record else None
    except (ValueError, KeyError):
        continue
    if payload is None:          # the headers entry: the payload one follows
        continue
    when = match.group(1).replace("T", " ")[:19]
    lane = record.get("resource-path", "").split("/")[1] if record.get("resource-path") else "?"
    if record.get("mediation-flow") == "REQUEST":
        print(f"{when}  {lane}  question  (request {record.get('request-id', '?')[:8]})")
        print(f"    {short(question(payload))}")
    else:
        print(f"{when}  {lane}  answer{tokens(payload)}")
        print(f"    {short(answer(payload))}")
    count += 1

if not count:
    print("No audit record yet: ask a question on the Audit log card first.")
