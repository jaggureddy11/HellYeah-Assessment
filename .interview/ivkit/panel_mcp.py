#!/usr/bin/env python3
"""One MCP server for the whole session.

Built-in tools come from the platform. Pack tools come from whatever the problem
pack declared. The candidate's agent sees one flat list and does not know or
care which is which. Every call is logged.

Speaks JSON-RPC over stdin and stdout, in both the handshake protocol
(2025-06-18) and the stateless one (2026-07-28). Stdout carries protocol only;
anything worth logging goes to stderr.
"""
from __future__ import annotations
import json, os, pathlib, sys, threading, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ivkit.tools import Registry, Tool, load_pack_tools     # noqa: E402

NEWEST = "2026-07-28"


def repo_root() -> pathlib.Path:
    env = os.environ.get("INTERVIEW_REPO")
    if env:
        return pathlib.Path(env).resolve()
    return HERE.parents[1]


def log(msg: str) -> None:
    sys.stderr.write(f"[panel] {msg}\n")
    sys.stderr.flush()


def send(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def client_protocol(msg: dict) -> str:
    meta = msg.get("_meta") or {}
    return (meta.get("io.modelcontextprotocol/protocolVersion")
            or (msg.get("params") or {}).get("protocolVersion")
            or NEWEST)


# ------------------------------------------------------------ built-in tools

def builtin_ask_panel(question: str = "") -> str:
    from ivkit import panel
    return panel.answer(repo_root(), question)


def builtin_time_left() -> str:
    from ivkit import pause, record
    heard = True
    if pause.session(repo_root()):
        # One quick read of the server's clock (spec §5.3). It changes the kit's
        # copy of that clock, never the session: time_left stays read-only for
        # the agent, and an answer older than the copy is refused (pause.apply).
        # With no answer it could read, the copy is said as the last known
        # (spec §5.2): the agent must not tell the candidate their clock is
        # stopped when it may run.
        heard = pause.sync_heard(repo_root(), timeout=3)[2]
    return record.status(repo_root(), heard=heard)


def build_registry() -> Registry:
    reg = Registry(repo_root())
    reg.add(Tool(
        "ask_panel",
        "Ask the interview panel a question about the exercise. The question is "
        "logged with a timestamp for the panel to read later. Returns the panel's "
        "FAQ together with the rules for answering from it; answer the candidate "
        "from those and only those. Asking costs the candidate nothing, and so "
        "does a question the panel declines to settle.",
        {"type": "object",
         "properties": {"question": {"type": "string",
                                     "description": "The question, in the candidate's own words."}},
         "required": ["question"]},
        builtin_ask_panel, "builtin"))
    reg.add(Tool(
        "time_left",
        "How long the candidate has left in this timed exercise, and how much has "
        "been recorded so far. Read-only. Worth checking before proposing anything "
        "large: when little time remains, prefer finishing and verifying what "
        "exists over starting something new.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        builtin_time_left, "builtin"))
    load_pack_tools(repo_root(), reg, log)
    return reg


# ------------------------------------------------------------ protocol

def handle(msg: dict, reg: Registry):
    mid, method = msg.get("id"), msg.get("method")

    if method in ("initialize", "server/discover"):
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": client_protocol(msg),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "interview-panel", "version": "2.0.0"}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"resultType": "complete", "tools": reg.list()}}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            text, is_err = reg.call(p.get("name", ""), p.get("arguments") or {})
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"no tool named {p.get('name')}"}}
        result = {"resultType": "complete",
                  "content": [{"type": "text", "text": text}]}
        if is_err:
            result["isError"] = True
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method {method}"}}


def watchdog() -> None:
    """While an agent session is open, its panel server keeps the recorder up:
    it restarts a recorder that died, which also covers the bell and a failed
    upload. Safe to run in every open agent at once (see record.ensure)."""
    from ivkit import record
    every = float(os.environ.get("INTERVIEW_WATCH") or 60)
    while True:
        time.sleep(every)
        if record.ensure(repo_root(), "mcp"):
            log("recorder was not running; restarted it")


def main() -> int:
    reg = build_registry()
    log(f"serving from {repo_root()} with tools: {', '.join(reg.tools)}")
    threading.Thread(target=watchdog, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out = handle(msg, reg)
        except Exception as e:
            log(f"handler crashed: {e!r}")
            out = {"jsonrpc": "2.0", "id": msg.get("id"),
                   "error": {"code": -32603, "message": "internal error"}}
        if out is not None:
            send(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
