"""Claude Code: the CLI and the VS Code extension, which share one store.

Store: ~/.claude/projects/<encoded path>/ (or CLAUDE_CONFIG_DIR), one .jsonl
transcript per session, each session with a directory holding its subagent
transcripts and any tool output too large to inline.

Wiring: hooks in .claude/settings.json, the panel in .mcp.json, both project
scope. Cursor runs these hooks too, with its own payloads; hook_log.py tells
them apart.

This module ships to the candidate's machine, where trace.py does not: parse()
imports it only when the panel runs it.
"""
from __future__ import annotations
import json, pathlib, re, shutil, subprocess

from . import base

# User turns the harness writes, not the candidate: background-task results,
# command output, messages from other agents, the interrupt marker.
HARNESS = re.compile(r"^(<(task-notification|local-command-\w+|bash-stdout|bash-stderr|cross-session-message|"
                     r"agent-message|teammate-message)\b|\[Request interrupted)")
REJECTED = re.compile(r"the user said:\s*(.+)", re.S | re.I)
REJECTION = re.compile(r"\s*The user doesn't want to (?:proceed|take this action)", re.I)
BACKGROUND_ID = re.compile(r"(?:with ID|shell ID|task ID|bash_id|shell_id)[:\s]+([A-Za-z0-9_-]{4,})", re.I)
VERSION = re.compile(r"\d+(?:\.\d+)+")


def _hooks(command: str, script: str) -> str:
    def h(event):
        return [{"hooks": [{"type": "command",
                            # both may contain spaces. Exit 2 blocks the prompt or the call, and python
                            # exits 2 when the script is gone (the folder moved since setup)
                            "command": f'"{command}" "{script}" --tool claude-code {event} || true',
                            "timeout": 5}]}]
    settings = {"hooks": {
        "SessionStart": h("session_start"),
        "UserPromptSubmit": h("user_prompt"),
        "PreToolUse": [{"matcher": "*", **h("tool_use")[0]}],
        "Stop": h("stop"),
        "SessionEnd": h("session_end"),
    }}
    return json.dumps(settings, indent=2) + "\n"


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _cc_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _notice(out: dict, t, text: str) -> None:
    from .. import trace
    tid = re.search(r"<task-id>\s*([^<\s]+)", text)
    if tid:
        out["notices"].append({"t": t, "id": tid.group(1), "status": trace._status(text)})


def _result_text(b: dict) -> str:
    c = b.get("content")
    if isinstance(c, list):
        c = "\n".join(x.get("text") or "" for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def _file(tr: dict, f: pathlib.Path, rel: str, actor: str, session: str, bad, out: dict) -> dict:
    """One transcript: prompts (or the subagent's brief), assistant text, tool
    calls with their full inputs and the time their result came back."""
    from .. import trace
    info = base.session_info("claude-code", session, rel, actor)
    calls: dict = {}
    for n, r in trace.rows(f, bad, rel):
        try:
            _record(tr, n, r, rel, actor, session, info, calls, out)
        except (AttributeError, TypeError, KeyError, ValueError):
            bad[rel] += 1                   # one record in a shape nobody foresaw; the rest of the file stands
    info["models"] = sorted(info["models"])
    return info


def _record(tr: dict, n: int, r: dict, rel: str, actor: str, session: str, info: dict,
            calls: dict, out: dict) -> None:
    from .. import trace
    from ..review import is_injected
    local = trace.epoch(r.get("timestamp"))
    t = trace.server_time(tr, local)
    if t is not None:
        info["first"] = t if info["first"] is None else min(info["first"], t)
        info["last"] = t if info["last"] is None else max(info["last"], t)
    ref = f"{rel}:{n}"
    typ = r.get("type")
    msg = _d(r.get("message"))
    info["cwd"] = info["cwd"] or r.get("cwd")
    if isinstance(r.get("version"), str) and VERSION.fullmatch(r["version"]):
        out["_versions"].add(r["version"])
    if typ == "attachment":
        a = _d(r.get("attachment"))
        if a.get("type") == "queued_command" and str(a.get("prompt", "")).lstrip().startswith("<task-notification"):
            _notice(out, t, a["prompt"])            # how a background job ended, delivered while the agent was busy
        if not actor.startswith("subagent") and a.get("type") == "queued_command" \
                and a.get("commandMode", "prompt") == "prompt" and isinstance(a.get("prompt"), str) \
                and not is_injected(a["prompt"]) and not HARNESS.match(a["prompt"].lstrip()):
            out["prompts"].append(base.prompt(t, local, "claude-code", a["prompt"], session, ref, queued=True))
        return
    if typ == "user":
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                res = _result_text(b)
                c = calls.get(b.get("tool_use_id"))
                if c is not None:
                    c["t_result"], c["error"] = t, bool(b.get("is_error"))
                    if c.get("background"):
                        m = BACKGROUND_ID.search(res)
                        c["bg_id"] = m.group(1) if m else None
                    if c.get("name") in trace.BACKGROUND_READERS:
                        c["result_head"] = res[:2000]
                # A rejected tool call carries what the candidate typed while rejecting it.
                # only a rejection: not an AskUserQuestion reply, not a command's own output
                fb = None
                if b.get("is_error") and REJECTION.match(res):
                    m = REJECTED.search(res)
                    fb = r.get("userFeedback") if isinstance(r.get("userFeedback"), str) \
                        else (m.group(1).strip() if m else None)
                if fb and not actor.startswith("subagent"):
                    out["events"].append(base.event(t, "claude-code", "rejection", "candidate", fb, ref,
                                                    session=session, of=c["ref"] if c else None,
                                                    tool=c["name"] if c else None))
        txt = _cc_text(content).strip()
        if txt.startswith("<task-notification"):     # a background job reporting how it ended
            _notice(out, t, txt)
        if not txt or r.get("isMeta") or r.get("isCompactSummary") or txt.startswith("<command-") \
                or is_injected(txt) or HARNESS.match(txt):
            return
        if actor.startswith("subagent"):
            if info["brief"] is None:
                info["brief"] = {"t": t, "text": txt, "ref": ref}
                out["events"].append(base.event(t, "claude-code", "brief", actor, txt, ref, session=session))
            return
        info["prompts"] += 1
        out["prompts"].append(base.prompt(t, local, "claude-code", txt, session, ref))
    elif typ == "assistant":
        if msg.get("model"):
            info["models"].add(str(msg["model"]))
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            out["events"].append(base.agent_text(t, "claude-code", actor, content, ref, session))
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                out["events"].append(base.agent_text(t, "claude-code", actor, b["text"], ref, session))
            elif b.get("type") == "tool_use":
                name, inp = b.get("name") or "?", b.get("input") or {}
                c = base.tool_call(t, "claude-code", actor, trace.summary(name, inp), ref, name=name, input=inp,
                                   op=trace.op_of(name, inp), session=session, id=b.get("id"),
                                   background=bool(isinstance(inp, dict) and inp.get("run_in_background")))
                calls[b.get("id")] = c
                info["calls"] += 1
                out["events"].append(c)


def _under(p: str, repo: pathlib.Path) -> bool:
    try:
        r = pathlib.Path(p).resolve()
    except Exception:
        return False
    return r == repo or repo in r.parents


class ClaudeCode(base.Adapter):
    name = "claude-code"
    title = "Claude Code"

    def detect(self) -> str | None:
        exe = shutil.which("claude")
        if not exe:
            return None
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return "installed"
        m = VERSION.search(r.stdout or "") if r.returncode == 0 else None
        return m.group(0) if m else "installed"

    def hook_files(self, python: str, hook_script: str, repo_root: str | None = None) -> dict[str, str]:
        script = hook_script if repo_root is not None else "$CLAUDE_PROJECT_DIR/" + hook_script
        return {".claude/settings.json": _hooks(python, script)}

    def mcp_files(self, python: str, mcp_script: str, repo_root: str | None = None) -> dict[str, str]:
        from .. import configs
        if repo_root is None:           # the VS Code extension loads project settings too
            return {".mcp.json": configs._mcp(python, "${CLAUDE_PROJECT_DIR}/" + mcp_script, "${CLAUDE_PROJECT_DIR}")}
        return {".mcp.json": configs._mcp(python, mcp_script, repo_root)}

    def collect(self, repo: pathlib.Path, out: pathlib.Path, save) -> tuple[list[str], list[str]]:
        """One .jsonl per session, so several tabs give several files. Each session
        also has a directory holding its subagent transcripts and any tool output too
        large to inline. All of it is collected, with the layout preserved."""
        from .. import paths as P
        repo = pathlib.Path(repo).resolve()
        enc = P.claude_project_dirname(repo)
        home = P.cli_history_roots()["Claude Code"]
        if not home.exists():
            return [], []

        def belongs(d: pathlib.Path) -> bool:
            """Exact name match is proof. Anything else, including a directory that
            merely starts with our encoding (a sibling repo such as <name>-old, or a
            session started in a subdirectory), must show a recorded cwd under this
            repository before a single file is taken."""
            if d.name == enc:
                return True
            for f in list(d.glob("*.jsonl"))[:6]:
                try:
                    with f.open(errors="ignore") as fh:
                        for _ in range(40):
                            line = fh.readline()
                            if not line:
                                break
                            try:
                                cwd = json.loads(line).get("cwd")
                            except (ValueError, AttributeError):
                                continue
                            if cwd:
                                return _under(cwd, repo)
                except OSError:
                    continue
            return False

        sessions = subs = spill = 0
        for d in home.glob("*"):
            if not d.is_dir() or not belongs(d):
                continue
            for f in d.glob("*.jsonl"):
                if f.is_file() and save("", f.name, f):
                    sessions += 1
            for sub in d.glob("*/subagents/*"):
                if not sub.is_file():
                    continue
                if save(str(sub.relative_to(d).parent), sub.name, sub) and sub.suffix == ".jsonl":
                    subs += 1
            for sp in d.glob("*/tool-results/*"):
                if not sp.is_file():
                    continue
                if save(str(sp.relative_to(d).parent), sp.name, sp):
                    spill += 1
        if not (sessions or subs):
            return [], []
        bits = [f"{sessions} session transcript(s)"]
        if subs:
            bits.append(f"{subs} subagent transcript(s)")
        if spill:
            bits.append(f"{spill} spilled tool output(s)")
        return ["Claude Code: " + ", ".join(bits)], []

    def parse(self, tr: dict, logs: pathlib.Path, bad) -> dict:
        from .. import trace
        out = {**base.parsed(), "_versions": set()}
        root = logs.parent
        for f in sorted(logs.glob("*.jsonl")):
            rel = str(f.relative_to(root))
            info = trace._guard(tr, rel, _file, tr, f, rel, "claude-code", f.stem, bad, out)
            if info:
                out["sessions"].append(info)
        spawns = {c.get("id"): c for c in out["events"] if c["kind"] == "tool_call" and c.get("op") == "agent"}
        for f in sorted(logs.glob("*/subagents/*.jsonl")):
            meta = {}
            try:
                meta = json.loads(f.with_suffix(".meta.json").read_text())
            except (OSError, ValueError):
                pass                    # no meta file: the subagent is listed without its brief
            spawn = spawns.get(meta.get("toolUseId")) or {}
            typ = meta.get("agentType") or (spawn.get("input") or {}).get("subagent_type") or "?"
            rel = str(f.relative_to(root))
            info = trace._guard(tr, rel, _file, tr, f, rel, f"subagent:{typ}", f.parent.parent.name, bad, out)
            if not info:
                continue
            info.update(type=typ, model=meta.get("model") or ",".join(info["models"]) or "?",
                        description=meta.get("description") or (spawn.get("input") or {}).get("description"),
                        spawn_ref=spawn.get("ref"), depth=meta.get("spawnDepth"))
            out["subagents"].append(info)
        out["coverage"]["version"] = ", ".join(sorted(out.pop("_versions"))) or None
        return out

    def from_hook(self, rec: dict) -> dict | None:
        from .. import trace
        name, inp = rec.get("tool_name") or "?", rec.get("tool_input")
        return {"name": name, "input": inp, "op": trace.op_of(name, inp)}


ADAPTER = ClaudeCode()
