"""The contract between an agent adapter and the rest of ivkit.

An adapter knows one tool: how to wire it into the repository (hooks, the
panel MCP server), how to find this repository's sessions in the tool's own
store, and how to turn what it finds into the timeline's events. Nothing
outside the adapter knows a tool's file formats.

Names. `Adapter.name` is used everywhere a tool is named: the directory under
.interview/agent_logs/, the `--tool` argument of hook_log.py, the `tool` field
of hook records, prompts and sessions, and the `src` of events.

    claude-code   codex   copilot   cursor

Times. Every `t` is on the server clock: pass the tool's own time, as epoch
seconds on the candidate's machine, through `trace.server_time(tr, local)`.
`local` keeps the machine time where the caller wants it. A record without a
usable time gets t=None; the timeline lists it last and says so.

What parse() returns (see `parsed()`):

  events       agent_text and tool_call events, built with agent_text() and
               tool_call()
  prompts      the candidate's prompts, built with prompt()
  sessions     one session_info() per session file or conversation
  subagents    session_info() for subagent transcripts, with `type`
  notices      harness notices (Claude Code background-task ends); [] elsewhere
  completions  lines an inline completion wrote, when the tool records them:
               {"t", "path", "text", "tool", "source"}, source "tab" for
               Cursor Tab
  completion_windows
               spans in which an unrecorded inline completion may have written
               code: {"tool", "start", "end", "what"}; start/end None means
               the whole session
  coverage     {"parsed": bool, "format": "ok" or "unrecognised: <what>",
               "version": str | None}; trace adds the counts

A tool_call carries, besides the display fields, what provenance needs,
already normalised so provenance never reads a tool's own input shapes:

  op        "edit", "command", "agent" or "other"
  writes    {path: text written} for an edit: the text the edit put in the
            file (new file content, the replacement text, patch + lines).
            Paths as the tool gave them; provenance maps them to the repo.
  command   the command line that actually ran, for a command
  applied   False when the tool says the call was cancelled, rejected,
            failed or undone before it could change anything
  id        the tool's call id, normalised so that it equals `tool_use_id`
            in this tool's hook records; the cross-check matches on it
  actor     the tool's name, "subagent:<type>", or "candidate" for a command
            the candidate ran through the tool (Codex `!cmd`); provenance
            never credits a candidate call to the agent

When parse() runs, tr["hooks"] already holds this submission's hook records
(deduplicated, each with its server time `t`), so an adapter can use its own
tool's records, e.g. Cursor's tab_edit lines as completions.

Hook records. hook_log.py is called as `<python> <hook_log.py> --tool <name>
<event>`, `event` one of:

  session_start  user_prompt  tool_use  tool_result  file_edit  tab_edit
  stop  session_end

and appends one JSON line to .interview/transcript.jsonl:

  {"ts", "tool", "event", "hook_event_name", "session_id", "transcript_path",
   "cwd", "prompt", "tool_name", "tool_input", "tool_use_id", "ids",
   "tool_version", ...}

`ids` holds whatever other identifiers the tool sends (conversation_id,
generation_id, turn_id). from_hook() turns one tool_use / file_edit /
tab_edit record into the tool_call fields, for calls the native store lost.
"""
from __future__ import annotations
import pathlib

EDIT, COMMAND, AGENT, OTHER = "edit", "command", "agent", "other"
HOOK_EVENTS = ("session_start", "user_prompt", "tool_use", "tool_result", "file_edit", "tab_edit",
               "stop", "session_end")


def event(t, src, kind, actor, text, ref, **extra) -> dict:
    return {"t": t, "src": src, "kind": kind, "actor": actor, "text": text, "ref": ref, **extra}


def agent_text(t, tool, actor, text, ref, session) -> dict:
    return event(t, tool, "agent_text", actor, text, ref, session=session)


def tool_call(t, tool, actor, summary, ref, *, name, input, op, session, id=None, writes=None,
              command=None, applied=True, t_result=None, error=None, clipped=False,
              background=False, cwd=None) -> dict:
    return event(t, tool, "tool_call", actor, summary, ref, name=name, input=input, op=op,
                 writes=writes, command=command, applied=applied, t_result=t_result, error=error,
                 session=session, id=id, clipped=clipped, background=background, cwd=cwd)


def prompt(t, local, tool, text, session, ref, queued=False) -> dict:
    return {"t": t, "local": local, "text": text, "session": session, "ref": ref, "queued": queued,
            "tool": tool}


def session_info(tool, session, file, actor, cwd=None) -> dict:
    """Filled in while parsing: first/last (server time), models (a set until
    parse returns, then a sorted list), calls and prompts (counts)."""
    return {"tool": tool, "session": session, "file": file, "actor": actor, "first": None, "last": None,
            "models": set(), "calls": 0, "prompts": 0, "brief": None, "cwd": cwd}


def parsed() -> dict:
    return {"events": [], "prompts": [], "sessions": [], "subagents": [], "notices": [],
            "completions": [], "completion_windows": [],
            "coverage": {"parsed": True, "format": "ok", "version": None}}


class Adapter:
    name = ""               # claude-code, codex, copilot, cursor
    title = ""              # as the candidate knows it: "Claude Code"
    supported = True        # listed as fully supported in the brief

    def detect(self) -> str | None:
        """The installed version, 'installed' when it cannot be read, or None."""
        return None

    def hook_files(self, python: str, hook_script: str, repo_root: str | None = None) -> dict[str, str]:
        """{repo-relative path: content} of this tool's project hook config.
        repo_root None: the build-time defaults, which cannot know the machine."""
        return {}

    def mcp_files(self, python: str, mcp_script: str, repo_root: str | None = None) -> dict[str, str]:
        """{repo-relative path: content} registering the panel server."""
        return {}

    def collect(self, repo: pathlib.Path, out: pathlib.Path, save) -> tuple[list[str], list[str]]:
        """Copy this repository's sessions from the tool's store into out/
        (.interview/agent_logs/<name>/) with save(sub, name, data), the
        collector's budgeted writer. Return (found, skipped): one line each
        for the candidate's printout."""
        return [], []

    def parse(self, tr: dict, logs: pathlib.Path, bad) -> dict:
        """logs is .interview/agent_logs/<name>/. bad is the torn-line counter."""
        return parsed()

    def from_hook(self, rec: dict) -> dict | None:
        """tool_call() keyword fields (name, input, op, writes, command) for a
        hook record whose call the native store does not have."""
        return None

    def cross_checkable(self, rec: dict) -> bool:
        """False for a hook record no native record can ever match (Cursor
        gives MCP tool hooks a fresh tool_use_id), so the cross-check skips it
        instead of calling the call lost."""
        return True
