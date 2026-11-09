#!/usr/bin/env python3
"""Every tool's hook: appends one JSON line per event to .interview/transcript.jsonl.

    <python> hook_log.py [--tool <name>] <event>

The project hook configs `interview setup` writes call it, each naming its
tool (claude-code, codex, copilot, cursor); without --tool it is Claude Code,
as in kits built before there was a choice. It only fires when the candidate's
tool runs project hooks, which most do only in a trusted folder. It is a bonus
tier on top of the working-tree snapshots, which do not depend on any tool.

Most tools' payloads carry no time, so the line is stamped here, in UTC, to
the millisecond: the tools' own records are timed in milliseconds.
Cursor runs its own hooks and the Claude-format ones in .claude/settings.json
for the same call; both lines are kept here, and trace reads them once. A
payload another tool sent through the Claude-format ones is recorded as that
tool's, with "via": "claude-settings" (VS Code's, with chat.useClaudeHooks on,
names no version and is known by its shape).

Contract this script must honour:
  - print NOTHING to stdout. On UserPromptSubmit, stdout is injected into the
    model's context; Cursor reads it as a permission answer, and anything but
    nothing or valid JSON blocks the action.
  - never exit non-zero. Exit code 2 blocks the action; a failure here must be
    invisible to the candidate's work.
"""
import sys, os, json, datetime, pathlib, re

MAX_FIELD = 4000
MAX_INPUT = 600             # per field of a tool's input
MAX_EDIT = 20000            # the text an edit writes: Cursor's Write carries the whole file
EDIT_FIELDS = ("content", "new_string", "file_text", "contents", "streamContent", "edits",
               # Copilot's edit tools; `input` is its apply_patch's patch
               "newString", "code", "input", "patch", "replacements", "newCode", "new_str")
# Nothing else caps this log, and every checkpoint carries all of it. Past this
# size edit text is clipped like any other field, and the record says so.
LOG_BUDGET = 20 * 1024 * 1024
IDS = ("conversation_id", "generation_id", "turn_id")
CALL_EVENTS = ("tool_use", "tool_result", "file_edit", "tab_edit")
# A payload that names another tool, arriving through the Claude-format hooks
# that tool also runs.
OTHER_TOOL = (("cursor_version", "cursor"), ("copilotVersion", "copilot"))
# VS Code (chat.useClaudeHooks) names no version there, but it alone sends an
# ISO `timestamp`, and its call ids carry __vscode-<n>
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d")


def clip(v, n=MAX_FIELD):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n} chars]"


def short(v):
    """An id or a path: kept as it is, bounded."""
    return None if v is None else clip(v, MAX_INPUT)


def tool_input(payload: dict, name, max_edit=MAX_EDIT):
    ti = payload.get("tool_input", {})
    if "tool_input" not in payload:             # Cursor's file and shell events carry the call at the top
        ti = {k: payload[k] for k in ("file_path", "edits", "command") if k in payload} or ti
    edit = name == "apply_patch"                # Codex: the patch is tool_input.command
    if isinstance(ti, dict):
        return {k: clip(v, max_edit if edit or k in EDIT_FIELDS else MAX_INPUT) for k, v in list(ti.items())[:12]}
    return clip(ti, max_edit if edit else MAX_INPUT)


def main():
    args = sys.argv[1:]
    tool = "claude-code"
    if args[:1] == ["--tool"]:
        tool, args = (args[1] if len(args) > 1 else tool), args[2:]
    event = args[0] if args else "unknown"
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        payload = {"_unparsed": clip(raw, 500)}

    via = None
    if tool == "claude-code":
        for key, other in OTHER_TOOL:
            if payload.get(key):
                tool, via = other, "claude-settings"
        stamp, use = payload.get("timestamp"), payload.get("tool_use_id")
        if tool == "claude-code" and ((isinstance(stamp, str) and ISO.match(stamp))
                                      or (isinstance(use, str) and "__vscode-" in use)):
            tool, via = "copilot", "claude-settings"
    # A kit's own copy sits in <repo>/.interview/ivkit/, and that repo is the
    # interview's whatever the tool says: Cursor sets CLAUDE_PROJECT_DIR to a
    # window's first folder, which need not be this one.
    here = pathlib.Path(__file__).resolve().parents[2]
    root = str(here) if (here / ".interview").is_dir() else (
        os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("INTERVIEW_REPO") or str(here))
    out = pathlib.Path(root) / ".interview" / "transcript.jsonl"
    try:
        over = out.stat().st_size > LOG_BUDGET
    except OSError:
        over = False                            # no log yet
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        "tool": tool,
        "event": event,
        "hook_event_name": short(payload.get("hook_event_name")),
        "session_id": short(payload.get("session_id") or payload.get("conversation_id")),
        "transcript_path": short(payload.get("transcript_path")),
        "cwd": short(payload.get("cwd")),
        "tool_use_id": short(payload.get("tool_use_id")),
        "ids": {k: short(payload[k]) for k in IDS if payload.get(k)},
        "tool_version": short(payload.get("cursor_version") or payload.get("copilotVersion")),
    }
    if via:
        rec["via"] = via
    if "_unparsed" in payload:
        rec["_unparsed"] = payload["_unparsed"]
    if event == "user_prompt":
        rec["prompt"] = clip(payload.get("prompt", ""))
    elif event in CALL_EVENTS:
        rec["tool_name"] = short(payload.get("tool_name"))
        if event != "tool_result":
            rec["tool_input"] = tool_input(payload, payload.get("tool_name"), MAX_INPUT if over else MAX_EDIT)
            if over:
                rec["clip"] = "budget"
        if payload.get("agent_type") or payload.get("agent_id"):
            rec["agent"] = short(payload.get("agent_type") or payload.get("agent_id"))
    elif event == "stop":
        rec["last_assistant_message"] = clip(payload.get("last_assistant_message", ""), 2000)

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8", "replace")
        # One write on an append descriptor: a line longer than the stdio buffer
        # would otherwise go out in pieces, between another tab's.
        fd = os.open(str(out), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            while line:
                line = line[os.write(fd, line):]
        finally:
            os.close(fd)
    except Exception:
        # A hook must never block the candidate's agent or print to stdout
        # (stdout is injected into the model's context). Swallow and move on.
        pass
    if event in ("session_start", "user_prompt", "tool_use"):
        watch(pathlib.Path(root))


def watch(root: pathlib.Path) -> None:
    """Restarts the recorder if it died. Silent: record.ensure prints nothing
    and starts the recorder detached, well inside the hook's timeout."""
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from ivkit import record
        record.ensure(root, "hook")
    except Exception:
        pass                        # silent whatever happens; the next hook tries again


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Same contract: silent, exit 0, whatever happened.
        pass
    sys.exit(0)
