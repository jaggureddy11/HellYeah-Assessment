"""GitHub Copilot in VS Code: Copilot Chat's Local harness, and the Copilot
harness, which shares the Copilot CLI's store.

Store (VS Code 1.138, microsoft/vscode 7debcd0e):

  <user data>/User/workspaceStorage/<wsId>/
    workspace.json          {"folder": "file:///..."} or {"workspace": <.code-workspace>}:
                            how a workspace is matched. wsId hashes the folder's
                            birth time, so it cannot be computed, and a re-cloned
                            repository gets a new one
    chatSessions/<id>.jsonl one chat as a mutation log, replayed below; <id>.json
                            is the pre-1.109 form, which a .jsonl supersedes
    chatEditingSessions/<id>/state.json   per chat: each file's full text when a
                            request first touched it, and every edit operation
    GitHub.copilot-chat/transcripts/   written only while some hook is on
    state.vscdb             the chat index
  <user data>/User/globalStorage/emptyWindowChatSessions/   chats with no folder
  $COPILOT_HOME or ~/.copilot/session-state/<id>/events.jsonl   the Copilot
                            harness and the Copilot CLI, one event per line

The Claude and Codex harnesses of the same Chat view write those tools' own
stores, which their adapters read; a cloud session leaves only an index entry.

Wiring: hooks in .github/hooks/ivkit.json, which Workspace Trust gates and the
user can switch off (chat.useHooks); the panel in .vscode/mcp.json.

VS Code writes a chat on its idle flush, about once a minute, and when the
window closes: the last minute before an automatic submission may be missing.
Inline completions, next-edit suggestions and inline chat (Cmd/Ctrl-I) leave no
record at all. Only a Copilot chat request or hook record in the session shows
Copilot was used; then the settings say whether suggestions were on, and inline
chat is taken to be at hand around that use.

What leaves the machine: a window on this folder alone gives its chats from
the session; any other window on it, or no window, gives only the requests
that worked here, stripped to what the parser reads; an editing session gives
only the files here; a Copilot CLI session, its records from the session. Of a
chat or CLI log in a format this cannot read, only its name, size and time.

This module ships to the candidate's machine, where trace.py does not: parse()
imports it only when the panel runs it.
"""
from __future__ import annotations
import collections, datetime, difflib, json, os, pathlib, re, shlex, shutil, sqlite3, subprocess, tempfile
from urllib.parse import unquote, urlparse

from . import base

APP_PACKAGE = pathlib.Path("/Applications/Visual Studio Code.app/Contents/Resources/app/package.json")
HOOK_FILE = ".github/hooks/ivkit.json"
HOOKS = (("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt"), ("PreToolUse", "tool_use"),
         ("PostToolUse", "tool_result"), ("Stop", "stop"))
# The chat index only, and of each entry only what says nothing: external
# (Cloud, Copilot CLI) sessions may be listed from other folders' windows.
STATE_KEYS = ("chat.ChatSessionStore.index",)
INDEX_FIELDS = ("isExternal", "isEmpty", "lastMessageDate", "timing", "lastResponseState", "initialLocation",
                "hasPendingEdits")
SETTINGS = ("github.copilot.enable", "editor.inlineSuggest.enabled", "github.copilot.nextEditSuggestions.enabled",
            "chat.disableAIFeatures")
ENABLE_DEFAULT = {"*": True, "plaintext": False, "markdown": False, "scminput": False}   # package.json
DEFAULT_PROFILE = "__default__profile__"
CLI_DIR, EMPTY_DIR = "copilot-cli", "empty-window"
VERSION = re.compile(r"\d+(?:\.\d+)+")
FENCE = re.compile(r"[\s`]*")
CLIP = re.compile(r"\.\.\.\[\+\d+ chars\]$")        # hook_log.clip's suffix
UNRECOGNISED = "unrecognised: "
SLACK = 5.0                 # seconds of clock and write latency around a span
SINCE_SLACK = 3600.0        # records last written this long before the session began are another project's
REQUEST_SLACK = 600.0       # a request this long before the session began still counts as the session's
# What the parser reads of a chat from any window but one on this folder
# alone, and of its requests; the rest holds titles, drafts, attachments,
# rendered prompts, tool output and summaries of earlier turns.
CHAT_KEEP = ("version", "sessionId", "creationDate", "initialLocation", "responderUsername")
REQUEST_KEEP = ("requestId", "timestamp", "message", "result", "modelState", "response", "agent", "modelId",
                "editedFileEvents", "isSystemInitiated", "shouldBeRemovedOnSend", "isHidden")
TOOL_KEEP = ("kind", "toolId", "toolCallId", "subAgentInvocationId", "toolSpecificData", "isConfirmed", "isComplete")
TOOL_DATA_KEEP = ("kind", "commandLine", "cwd", "terminalCommandState", "isBackground", "command", "agentName",
                  "startedAt", "duration", "description", "modelName", "rawInput")
EDIT_KINDS = ("textEditGroup", "notebookEditGroup", "codeblockUri")
NEVER_STORED = ("report_voice_progress",)           # toolCallingLoop.ts strips it from the stored rounds
INLINE_CHAT = "Copilot inline chat or completions: unrecorded"
SUGGESTIONS = "Copilot inline suggestions on"
ACTIVITY_MARGIN = 300.0     # seconds either side of Copilot's recorded use that inline chat's window takes in
NO_EVIDENCE = ("VS Code was used for this folder; Copilot's inline chat and completions leave no record, and "
               "nothing shows Copilot was active")
LOG_ONLY = ("Copilot's extension was running in VS Code during the session, but no Copilot chat request or hook "
            "record shows Copilot was used; its inline chat and completions leave no record, so lines they may "
            "have written read as typed")
LOG_ACTIVITY = "vscode_logs.json"
# The Copilot Chat extension's own logs, per window: written while it runs.
# Their times say it was active; what they say is never read.
EXT_LOGS = ("logs/*/window*/exthost/GitHub.copilot-chat/*.log", "logs/*/window*/exthost/github.copilot-chat/*.log",
            "logs/*/window*/exthost/output_logging_*/*GitHub Copilot*.log")

# The part's toolId as VS Code stores it, to the name the model and the hooks
# use (extensions/copilot/src/extension/tools/common/toolNames.ts). Core tools
# (run_in_terminal, runSubagent), MCP and extension tools use one name for both.
MODEL_NAME = {
    "copilot_applyPatch": "apply_patch", "copilot_searchCodebase": "semantic_search",
    "copilot_searchWorkspaceSymbols": "search_workspace_symbols", "copilot_getVSCodeAPI": "get_vscode_api",
    "copilot_findFiles": "file_search", "copilot_findTextInFiles": "grep_search",
    "copilot_readFile": "read_file", "copilot_viewImage": "view_image", "copilot_listDirectory": "list_dir",
    "copilot_getErrors": "get_errors", "copilot_getChangedFiles": "get_changed_files",
    "copilot_readProjectStructure": "read_project_structure",
    "copilot_createNewWorkspace": "create_new_workspace",
    "copilot_createNewJupyterNotebook": "create_new_jupyter_notebook",
    "copilot_insertEdit": "insert_edit_into_file", "copilot_createFile": "create_file",
    "copilot_replaceString": "replace_string_in_file",
    "copilot_multiReplaceString": "multi_replace_string_in_file", "copilot_editNotebook": "edit_notebook_file",
    "copilot_runNotebookCell": "run_notebook_cell",
    "copilot_readNotebookCellOutput": "read_notebook_cell_output",
    "copilot_installExtension": "install_extension", "copilot_fetchWebPage": "fetch_webpage",
    "copilot_memory": "memory", "copilot_findTestFiles": "test_search", "copilot_githubRepo": "github_repo",
    "copilot_githubTextSearch": "github_text_search", "copilot_createDirectory": "create_directory",
    "copilot_runVscodeCommand": "run_vscode_command", "copilot_editFiles": "edit_files",
    "copilot_switchAgent": "switch_agent", "copilot_resolveMemoryFileUri": "resolve_memory_file_uri",
    "copilot_sessionStoreSql": "session_store_sql",
}
# Edit tools, by the model's name (Local harness, its hooks) or the Copilot
# CLI's: (path fields, fields holding the new text)
EDIT_INPUTS = {
    "create_file": (("filePath",), ("content",)), "replace_string_in_file": (("filePath",), ("newString",)),
    "insert_edit_into_file": (("filePath",), ("code",)), "edit_notebook_file": (("filePath",), ("newCode",)),
    "create": (("path", "file_path"), ("file_text", "content")),
    "edit": (("path", "file_path"), ("new_str", "new_string")),
    "str_replace": (("path", "file_path"), ("new_str", "new_string")), "insert": (("path",), ("new_str",)),
}
# The Copilot CLI maps its tools to Claude's names in PascalCase hook files
ALIASES = {"Bash": "bash", "Write": "create", "Edit": "edit", "Read": "view", "Agent": "task"}
COMMANDS = ("run_in_terminal", "send_to_terminal", "create_and_run_task", "run_task", "runTests",
            "bash", "powershell", "write_bash", "write_powershell")
AGENTS = ("runSubagent", "search_subagent", "explore_subagent", "execution_subagent", "task")
PRIVATE = ("_pending", "_orphan", "_local", "_term_time", "_info", "_applied", "_model", "_whole")


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _ms(v) -> float | None:
    """VS Code stamps epoch milliseconds."""
    return v / 1000.0 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def _epoch(v) -> float | None:
    """The Copilot CLI's ISO 8601 stamps (any fraction, Z or an offset; none
    is local time). trace.epoch's rule, here because collect runs where trace
    is not."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$", s)
    if m:                                   # 3.9 takes only 3 or 6 digits of fraction
        s = m.group(1) + "." + (m.group(2) + "000000")[:6] + m.group(3)
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    return (d if d.tzinfo else d.astimezone()).timestamp()


def _path(u) -> str | None:
    """A file URI as VS Code stores it (UriComponents, or a string) to a path.
    Any other scheme (untitled:, vscode-remote:, vscode-notebook-cell:) is not a
    file on this machine: None."""
    if isinstance(u, dict):
        if u.get("scheme") not in (None, "file"):
            return None
        p = u.get("fsPath") or u.get("path")
    elif isinstance(u, str):
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]+):", u)
        if u.startswith("file:"):
            p = unquote(urlparse(u).path)
        elif m and len(m.group(1)) > 1:          # a one-letter scheme is a Windows drive
            return None
        else:
            p = u
    else:
        return None
    if not isinstance(p, str) or not p:
        return None
    return p[1:] if re.match(r"^/[A-Za-z]:[/\\]", p) else p     # /c:/... on Windows


def _under(p, repo: pathlib.Path) -> bool:
    """An absolute path in the repository. A relative one would resolve against
    wherever collect runs, which is the repository itself."""
    if not p:
        return False
    try:
        q = pathlib.Path(p)
        if not q.is_absolute():
            return False
        r = q.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return r == repo or repo in r.parents


def _load(p: pathlib.Path):
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _folder(wj: pathlib.Path) -> str | None:
    """The local folder a workspace directory belongs to; None for a remote or
    multi-root workspace."""
    v = _d(_load(wj)).get("folder")
    return _path(v) if isinstance(v, str) and v.startswith("file:") else None


_COMMA = re.compile(r"\s*[}\]]")


def _jsonc(text: str):
    """settings.json is JSON with comments and trailing commas."""
    def scan(s: str, skip) -> str:
        out, i = [], 0
        while i < len(s):
            if s[i] == '"':
                j = i + 1
                while j < len(s) and s[j] != '"':
                    j += 2 if s[j] == "\\" else 1
                out.append(s[i:j + 1])
                i = j + 1
                continue
            k = skip(s, i)
            if k > i:
                i = k
                continue
            out.append(s[i])
            i += 1
        return "".join(out)

    def comment(s: str, i: int) -> int:
        if s.startswith("//", i):
            j = s.find("\n", i)
            return len(s) if j < 0 else j
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            return len(s) if j < 0 else j + 2
        return i

    def comma(s: str, i: int) -> int:
        return i + 1 if s[i] == "," and _COMMA.match(s, i + 1) else i
    return json.loads(scan(scan(text, comment), comma))


def _folders(wj: pathlib.Path) -> list[str]:
    """Every local folder a workspace directory holds: its folder, or the
    folders its .code-workspace file lists (relative to that file)."""
    d = _d(_load(wj))
    f = d.get("folder")
    if isinstance(f, str):
        p = _path(f) if f.startswith("file:") else None
        return [p] if p else []
    w = d.get("workspace")
    cw = _path(w) if isinstance(w, str) and w.startswith("file:") else None
    if not cw:
        return []
    try:
        doc = _d(_jsonc(pathlib.Path(cw).read_text(encoding="utf-8", errors="replace")))
    except (OSError, ValueError):
        return []
    out = []
    for x in doc.get("folders") or []:
        x = _d(x)
        if isinstance(x.get("path"), str):
            q = pathlib.Path(x["path"])
            out.append(str(q if q.is_absolute() else pathlib.Path(cw).parent / q))
        elif isinstance(x.get("uri"), str) and _path(x["uri"]):
            out.append(_path(x["uri"]))
    return out


def _relation(folders: list[str], repo: pathlib.Path) -> str | None:
    """"full" when the workspace is the repository alone; "part" when a folder
    of it holds the repository or is inside it, or is the repository beside
    other folders, so only the requests that worked here are this interview's."""
    best = None
    for f in folders:
        try:
            r = pathlib.Path(f).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if r == repo and len(folders) == 1:
            return "full"
        if r == repo or repo in r.parents or r in repo.parents:
            best = "part"
    return best


def user_data_dirs() -> list[pathlib.Path]:
    """Where VS Code keeps User/. A `--user-data-dir` is invisible from
    outside the editor, so whoever used one (and the tests) names it in
    IVKIT_VSCODE_USER_DATA_DIR; VSCODE_PORTABLE is portable mode's own."""
    if os.environ.get("IVKIT_VSCODE_USER_DATA_DIR"):
        return [pathlib.Path(os.environ["IVKIT_VSCODE_USER_DATA_DIR"])]
    if os.environ.get("VSCODE_PORTABLE"):
        return [pathlib.Path(os.environ["VSCODE_PORTABLE"]) / "user-data"]
    from .. import paths as P
    out: list[pathlib.Path] = []
    for b in P.app_support():
        for name in ("Code", "Code - Insiders"):
            if b / name not in out:
                out.append(b / name)
    return out


def copilot_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("COPILOT_HOME") or pathlib.Path.home() / ".copilot")


# ---------------------------------------------------------------- tool inputs

def patch_writes(text: str) -> dict[str, str]:
    """{path: its + lines} from a V4A patch, apply_patch in both harnesses. A
    deleted file writes nothing; a moved one writes under its new path."""
    out: dict[str, list[str]] = {}
    cur, name = None, None
    for line in (text or "").splitlines():
        m = re.match(r"\*\*\* (?:Add|Update) File: (.+)$", line)
        mv = re.match(r"\*\*\* Move to: (.+)$", line)
        if m:
            name = m.group(1).strip()
            cur = out.setdefault(name, [])
        elif mv and cur is not None:
            new = mv.group(1).strip()
            moved = out.pop(name, [])
            cur = out.setdefault(new, [])
            cur.extend(moved)
            name = new
        elif line.startswith("*** "):
            cur, name = None, None
        elif cur is not None and line.startswith("+"):
            cur.append(line[1:])
    return {p: "\n".join(v) for p, v in out.items()}


def _objects(v) -> list[dict]:
    """A list of objects, also as the JSON text hook_log clips it to: the whole
    objects count, the cut one does not."""
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if not isinstance(v, str):
        return []
    dec, out = json.JSONDecoder(), []
    i = v.find("{")
    while i >= 0:
        try:
            o, end = dec.raw_decode(v, i)
        except ValueError:
            break
        if isinstance(o, dict):
            out.append(o)
        i = v.find("{", end)
    return out


def _unclip(v):
    """Text hook_log cut short: the whole lines before the cut."""
    m = CLIP.search(v) if isinstance(v, str) else None
    if not m:
        return v
    head = v[:m.start()]
    return head.rsplit("\n", 1)[0] if "\n" in head else ""


def _first(d: dict, keys: tuple):
    return next((d[k] for k in keys if isinstance(d.get(k), str)), None)


def _fields(name: str, inp) -> dict:
    """op, and writes or command, from a tool's own input."""
    d = _d(inp)
    name = ALIASES.get(name, name)
    if name == "str_replace_editor":             # the Copilot CLI's older combined tool
        if d.get("command") == "view":
            return {"op": base.OTHER}
        name = "create" if d.get("command") == "create" else "edit"
    if name == "apply_patch":
        text = next((v for v in (d.get("input"), d.get("patch"), inp) if isinstance(v, str)), "")
        return {"op": base.EDIT, "writes": patch_writes(text)}
    if name == "multi_replace_string_in_file":
        w: dict[str, str] = {}
        for r in _objects(d.get("replacements")):
            p, t = r.get("filePath"), r.get("newString")
            if isinstance(p, str) and isinstance(t, str):
                w[p] = w[p] + "\n" + t if p in w else t
        return {"op": base.EDIT, "writes": w}
    if name in EDIT_INPUTS:
        pk, tk = EDIT_INPUTS[name]
        p, t = _first(d, pk), _first(d, tk)
        return {"op": base.EDIT, "writes": {p: t or ""} if p else {}}
    if name in COMMANDS:
        v = _first(d, ("command", "input"))
        return {"op": base.COMMAND, "command": v if v is not None else json.dumps(inp, ensure_ascii=False, default=str)}
    if name in AGENTS:
        return {"op": base.AGENT}
    return {"op": base.OTHER}


def _same(a: str, b: str) -> bool:
    """One file, as the model named it and as VS Code resolved it."""
    a, b = a.replace("\\", "/"), b.replace("\\", "/")
    a, b = (x[2:] if x.startswith("./") else x for x in (a, b))
    return a == b or b.endswith("/" + a) or a.endswith("/" + b)


def _merge(name: str, applied: dict[str, list[str]], model: dict | None, whole: dict | None) -> dict[str, str]:
    """{path: text} for one edit call, for the files VS Code shows it edited
    and no others: the model's text for a file it never reached (no match, a
    failed item of a multi-replace) is not in the tree.

    The applied text is what VS Code put in the file, but trimmed: a replace
    applies only what old and new do not share (getIdenticalChars), often a
    fragment of a line. So the model's own new text, whole lines, and the whole
    lines the editing session's timeline shows changed are added to it.
    apply_patch applies a whole-document replace, so there the patch's + lines,
    else the timeline's, stand for it instead."""
    model = {k: v for k, v in (model or {}).items() if isinstance(k, str) and isinstance(v, str)}
    out: dict[str, str] = {}
    for path, texts in applied.items():
        mine = next((m for m in model if _same(m, path)), None)
        mt = model[mine].split("\n") if mine is not None else None
        wl = list((whole or {}).get(path) or [])
        got: list[str] = []

        def add(lines, got=got) -> None:
            for ln in lines:
                if ln not in got:
                    got.append(ln)
        if name == "apply_patch":
            add(mt if mt is not None else wl or "\n".join(texts).split("\n"))
        else:
            add("\n".join(texts).split("\n") if texts else [])
            add(mt or [])
        add(wl)
        out[path] = "\n".join(got)
    return out


def _summary(name: str, inp, writes, command) -> str:
    from .. import trace
    if writes:
        ps = list(writes)
        return ps[0] + (f" (+{len(ps) - 1} more)" if len(ps) > 1 else "")
    if command:
        return command
    if isinstance(inp, dict) and isinstance(inp.get("filePath"), str):
        return inp["filePath"]
    return trace.summary(name, inp)


# ---------------------------------------------------------------- the editing session's timeline

_EOL = re.compile(r"\r\n|\r|\n")


def _apply_edits(text: str, edits) -> str:
    """TextEdits, 1-based line and column ranges on the document before the
    batch, applied as VS Code's text buffer does."""
    starts = [0] + [m.end() for m in _EOL.finditer(text)]
    ends = [m.start() for m in _EOL.finditer(text)] + [len(text)]

    def off(line, col) -> int:
        i = min(max(int(line) - 1, 0), len(starts) - 1)
        return max(starts[i], min(starts[i] + int(col) - 1, ends[i]))
    spans = []
    for e in edits if isinstance(edits, list) else []:
        r, t = _d(_d(e).get("range")), _d(e).get("text")
        spans.append((off(r["startLineNumber"], r["startColumn"]), off(r["endLineNumber"], r["endColumn"]),
                      t if isinstance(t, str) else ""))
    for a, b, t in sorted(spans, reverse=True):
        text = text[:a] + t + text[b:]
    return text


def _added(before: str, after: str) -> list[str]:
    """The lines a change put in the file, as a line diff shows them."""
    a, b = before.splitlines(), after.splitlines()
    out = []
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out += b[j1:j2]
    return out


def timeline_lines(state) -> dict[tuple, list[str]]:
    """{(path, requestId): lines added} from a chatEditingSessions state.json
    (chatEditingCheckpointTimelineImpl.ts): each file's text from its baseline,
    with the operations replayed in epoch order. Whole lines, where the chat
    file holds only the fragment a replace applied. A shape it cannot follow
    gives nothing, never an error."""
    tl = _d(_d(state).get("timeline"))
    out: dict[tuple, list[str]] = {}
    try:
        evs = []
        for item in tl.get("fileBaselines") or []:
            b = _d(item[1] if isinstance(item, list) and len(item) == 2 else item)
            p = _path(b.get("uri"))
            if p and isinstance(b.get("content"), str) and isinstance(b.get("epoch"), (int, float)):
                evs.append((b["epoch"], 0, "baseline", p, b))
        for op in tl.get("operations") or []:
            op = _d(op)
            p = _path(op.get("uri")) or _path(op.get("oldUri"))
            if p and isinstance(op.get("epoch"), (int, float)):
                evs.append((op["epoch"], 1, op.get("type"), p, op))
        docs: dict[str, str | None] = {}
        for _, _, typ, p, x in sorted(evs, key=lambda e: (e[0], e[1])):
            key = (p, x.get("requestId"))
            if typ == "baseline":
                docs[p] = x["content"]
            elif typ == "create":
                docs[p] = x.get("initialContent") if isinstance(x.get("initialContent"), str) else ""
                out.setdefault(key, []).extend(_added("", docs[p]))
            elif typ == "delete":
                docs.pop(p, None)
            elif typ == "rename":
                new = _path(x.get("newUri"))
                if new:
                    docs[new] = docs.pop(p, None)
            elif typ == "textEdit":
                if x.get("cellIndex") is not None or docs.get(p) is None:
                    docs.pop(p, None)            # a notebook cell, or a file whose text is unknown
                    continue
                new = _apply_edits(docs[p], x.get("edits"))
                out.setdefault(key, []).extend(_added(docs[p], new))
                docs[p] = new
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {}
    return out


# ---------------------------------------------------------------- the Local harness

def _check(state) -> str:
    if not isinstance(state, dict):
        return UNRECOGNISED + "the chat is not an object"
    if "version" in state and state["version"] not in (1, 2, 3):
        return UNRECOGNISED + f"session version {state['version']!r}, not 1-3"
    if not isinstance(state.get("requests"), list):
        return UNRECOGNISED + "no requests list"
    return "ok"


def _mark(v, n: int, deep: bool) -> None:
    """Stamps the log line that wrote a request or a part on it."""
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, dict):
            x["_line"] = n
            if deep:
                _mark(x.get("response"), n, False)


def replay(path: pathlib.Path, bad, label: str) -> tuple[dict | None, list[dict], str]:
    """Replays a chat's mutation log (objectMutationLog.ts). Kind 0 is the whole
    chat and must come first; 1 sets the value at path k; 2 pushes v onto the
    array at k, cutting it to length i first when i is given; 3 deletes.

    Returns the final chat, every request the log ever held in first-seen
    order, and the format verdict. A request cut away by a checkpoint restore
    or a resend is kept, with _removed set: until VS Code compacts the file
    (past 1024 entries) the log still holds it. Requests and parts carry _line,
    the log line that last wrote them; requests also _first_line."""
    state = None
    seen: dict[str, dict] = {}
    first: dict[str, int] = {}

    def register(n: int) -> None:
        for r in state.get("requests") or [] if isinstance(state, dict) else []:
            if isinstance(r, dict) and isinstance(r.get("requestId"), str):
                first.setdefault(r["requestId"], n)
                seen[r["requestId"]] = r            # a re-pushed copy is the newer one

    fh = None
    try:
        with open(path, "rb") as fh:
            for n, raw in enumerate(fh, 1):
                s = raw.decode("utf-8", "replace").strip()
                if not s:
                    continue
                try:
                    e = json.loads(s)
                except ValueError:
                    bad[label] += 1                 # a torn line, as a crash mid-write leaves
                    continue
                if not isinstance(e, dict):
                    bad[label] += 1
                    continue
                kind = e.get("kind")
                if state is None and kind != 0:
                    return None, [], UNRECOGNISED + f"the log starts with kind {kind!r}, not 0 (initial)"
                try:
                    if kind == 0:
                        state = e["v"]
                        _mark(state.get("requests"), n, True)
                        register(n)
                        continue
                    k = e["k"]
                    if not isinstance(k, list) or not k or kind not in (1, 2, 3):
                        raise TypeError(kind)
                    cur = state
                    for key in k[:-1]:
                        cur = cur[key]
                    last = k[-1]
                    if kind == 1:
                        cur[last] = e.get("v")
                        if k[:1] == ["requests"] and len(k) == 2:
                            _mark(e.get("v"), n, True)
                    elif kind == 3:
                        cur[last] = None
                    else:
                        arr = cur[last] if isinstance(cur, list) else cur.get(last)
                        arr = arr if isinstance(arr, list) else []
                        if isinstance(e.get("i"), int):
                            del arr[e["i"]:]
                        arr.extend(e.get("v") or [])
                        cur[last] = arr
                        if k == ["requests"] or last == "response":
                            _mark(e.get("v"), n, k == ["requests"])
                except (KeyError, IndexError, TypeError, AttributeError):
                    bad[label] += 1                 # a path this state does not have: that entry is lost
                    continue
                if k[:1] == ["requests"] and len(k) <= 2:
                    register(n)
    except OSError as e:
        if fh is not None:                          # a failed open is a verdict; a failed read is not
            raise
        return None, [], UNRECOGNISED + f"cannot open ({type(e).__name__})"
    if state is None:
        return None, [], "ok"                       # nothing but torn lines
    fmt = _check(state)
    if fmt != "ok":
        return None, [], fmt
    final = {r.get("requestId") for r in state["requests"] if isinstance(r, dict)}
    for rid, r in seen.items():
        r["_removed"] = rid not in final
        r["_first_line"] = first[rid]
    return state, list(seen.values()), "ok"


def _flat(path: pathlib.Path, bad, label: str) -> tuple[dict | None, list[dict], str]:
    """A pre-1.109 chat: one JSON object, version 1 (unversioned) to 3."""
    state = _load(path)
    if state is None:
        bad[label] += 1
        return None, [], "ok"
    fmt = _check(state)
    if fmt != "ok":
        return None, [], fmt
    return state, [r for r in state["requests"] if isinstance(r, dict)], "ok"


def _message(m) -> str:
    if isinstance(m, str):                       # the oldest form
        return m
    return m["text"] if isinstance(m, dict) and isinstance(m.get("text"), str) else ""


def _is_md(p) -> bool:
    """Markdown is stored as a bare IMarkdownString, with no kind."""
    return isinstance(p, dict) and (p.get("kind") in (None, "markdownContent")) and \
        isinstance(p.get("value", _d(p.get("content")).get("value")), str)


def _md_text(p: dict) -> str:
    return p.get("value") if isinstance(p.get("value"), str) else _d(p.get("content")).get("value") or ""


def _cut(parts: list, stop) -> int:
    """Where the response a restore to undo stop `stop` keeps ends
    (chatModel.ts ResponseView): before the stop, and before the fence that
    opens the edit when one precedes it."""
    idx = next((j for j, p in enumerate(parts) if _d(p).get("kind") == "undoStop" and _d(p).get("id") == stop), -1)
    if not stop or idx < 0:
        return len(parts)
    if idx + 1 < len(parts) and _d(parts[idx + 1]).get("kind") == "codeblockUri" and idx > 0 and _is_md(parts[idx - 1]):
        idx -= 1
    return idx


def _args(c: dict):
    a = c.get("arguments")
    if isinstance(a, str):
        try:
            return json.loads(a)
        except ValueError:
            return a
    return a if a is not None else {}


def _hint(parts: list, j: int):
    """What a tool part says it touched: the command a terminal part ran, or
    the files of the edit parts that follow it."""
    tsd = _d(parts[j].get("toolSpecificData"))
    if tsd.get("kind") == "terminal":
        return "cmd", {v for v in _d(tsd.get("commandLine")).values() if isinstance(v, str)}
    paths = set()
    for q in parts[j + 1:]:
        q = _d(q)
        if q.get("kind") == "toolInvocationSerialized":
            break
        if q.get("kind") in ("textEditGroup", "notebookEditGroup", "codeblockUri") and _path(q.get("uri")):
            paths.add(_path(q.get("uri")))
    return ("paths", paths) if paths else None


def _fits(call: dict, hint) -> bool:
    if not hint:
        return False
    args = _d(_args(call))
    if hint[0] == "cmd":
        return args.get("command") in hint[1]
    named = set(_fields(call.get("name") or "", _args(call)).get("writes") or {})
    named |= {args[k] for k in ("filePath", "path") if isinstance(args.get(k), str)}
    return any(_same(a, b) for a in named for b in hint[1])


def _match(parts: list, calls: list, hooked) -> dict[int, int]:
    """part index -> index into the request's round calls. A part streamed
    from the model carries the model's call id without __vscode-<n>. The
    others carry a VS Code uuid, and arrive in no fixed order: a tool that
    streams its input gets its part while the model is still writing, ahead
    of calls it made first. Each goes to an unused call of its name, the one
    that names what the part touched, then one the hook log saw run, then the
    first."""
    tools = [j for j, p in enumerate(parts) if _d(p).get("kind") == "toolInvocationSerialized"
             and not p.get("subAgentInvocationId")]
    by_base: dict[str, int] = {}
    for n, (_, c) in enumerate(calls):
        by_base.setdefault(str(c.get("id") or "").split("__vscode-")[0], n)
    got: dict[int, int] = {}
    for j in tools:
        n = by_base.get(parts[j].get("toolCallId"))
        if n is not None and n not in got.values():
            got[j] = n
    used = set(got.values())
    for j in tools:
        if j in got:
            continue
        name = MODEL_NAME.get(parts[j].get("toolId"), parts[j].get("toolId"))
        cands = [n for n in range(len(calls)) if n not in used and calls[n][1].get("name") == name]
        if not cands:
            continue
        hint = _hint(parts, j)
        n = min(cands, key=lambda n: (not _fits(calls[n][1], hint), calls[n][1].get("id") not in hooked, n))
        got[j] = n
        used.add(n)
    return got


def _part_input(p: dict):
    """A call's input when no round records it: MCP and extension tools keep it."""
    tsd = _d(p.get("toolSpecificData"))
    if tsd.get("kind") == "input" and tsd.get("rawInput") is not None:
        return tsd["rawInput"]
    rd = p.get("resultDetails")
    if isinstance(rd, dict) and isinstance(rd.get("input"), str):
        try:
            return json.loads(rd["input"])
        except ValueError:
            return rd["input"]
    return {}


def _denied(c) -> bool:
    """ToolConfirmKind Denied (0) or Skipped (5); a bare false before 1.104.
    VS Code itself reads a missing value as Denied (chatService.ts): a part
    stored mid-stream or while it waited for approval."""
    return c is None or c is False or (isinstance(c, dict) and c.get("type") in (0, 5))


def _edit_texts(edits) -> list[str]:
    """The text of every TextEdit, of the text edits inside notebook cell
    operations, and of the cells a notebook replace puts in, in order."""
    out = []
    for batch in edits if isinstance(edits, list) else []:
        for e in batch if isinstance(batch, list) else [batch]:
            e = _d(e)
            t = e.get("text") if isinstance(e.get("text"), str) else _d(e.get("edit")).get("text")
            if isinstance(t, str):
                out.append(t)
            for cell in e.get("cells") or [] if isinstance(e.get("cells"), list) else []:
                src = _d(cell).get("source")
                if isinstance(src, list):
                    src = "".join(x for x in src if isinstance(x, str))
                if isinstance(src, str):
                    out.append(src)
    return out


def _touch(info: dict, t) -> None:
    if t is not None:
        info["first"] = t if info["first"] is None else min(info["first"], t)
        info["last"] = t if info["last"] is None else max(info["last"], t)


def _chat(tr: dict, f: pathlib.Path, rel: str, cwd, bad, out: dict, ctx: dict):
    """One Local-harness chat: prompts, agent text, and tool calls with what
    they wrote or ran. Returns its sessions, one per Copilot conversation id,
    which is what the hooks send as session_id (not the chat file's id).

    A tool part with no round call (a subagent's, or in a request whose rounds
    were not stored) is left _pending for parse to pair with a hook record; a
    round call with no part is kept, _orphan, only if a hook saw it run."""
    from .. import trace
    log = f.suffix == ".jsonl"
    state, reqs, fmt = replay(f, bad, rel) if log else _flat(f, bad, rel)
    if fmt != "ok":
        return {"format": fmt[len(UNRECOGNISED):]}
    if state is None:
        return None
    sid = state["sessionId"] if isinstance(state.get("sessionId"), str) else f.stem
    cwd = cwd or _path(state.get("workingDirectory"))
    timeline = timeline_lines(_load(f.parent.parent / "chatEditingSessions" / sid / "state.json"))
    infos: dict[str, dict] = {}

    def info_of(session: str) -> dict:
        if session not in infos:
            infos[session] = base.session_info("copilot", session, rel, "copilot", cwd=cwd)
            infos[session]["chat_session"] = sid
        return infos[session]
    # a request with no metadata belongs to the conversation of the one before
    cur = next((m for r in reqs for m in [_d(_d(r.get("result")).get("metadata")).get("sessionId")]
                if isinstance(m, str) and m), None)
    subagents: dict[str, dict] = {}
    pending: dict[str, list[dict]] = collections.defaultdict(list)   # path -> edits not yet kept or undone
    st = lambda local: trace.server_time(tr, local)
    starts_of = [st(_ms(r.get("timestamp"))) for r in reqs]

    for i, r in enumerate(reqs):
        md = _d(_d(r.get("result")).get("metadata"))
        cur = md["sessionId"] if isinstance(md.get("sessionId"), str) and md["sessionId"] else cur
        session = cur or sid
        info = info_of(session)
        local = _ms(r.get("timestamp"))
        t0 = st(local)
        sbr = _d(r.get("shouldBeRemovedOnSend"))
        # removed from the chat, or marked for removal by a restore to before it
        gone = bool(r.get("_removed") or r.get("isHidden") or (sbr and not sbr.get("afterUndoStop")))
        where = (lambda x, j=None, r=r: f"{rel}:{x.get('_line') or r.get('_first_line')}") if log else \
            (lambda x, j=None, i=i: f"{rel}#{i}" + (f".{j}" if j is not None else ""))
        if isinstance(r.get("modelId"), str):
            info["models"].add(r["modelId"])
        ver = _d(r.get("agent")).get("extensionVersion")
        if isinstance(ver, str) and ver:
            ctx["versions"].add(f"copilot-chat {ver}")
        # what the candidate did with the edits still pending: 1 keep, 2 undo, 3 changed them.
        # An edit is on disk from the moment it is applied, so an undone one
        # still wrote the file: it stays applied, and says it was undone.
        for ev in r.get("editedFileEvents") or []:
            p, kind = _path(_d(ev).get("uri")), _d(ev).get("eventKind")
            if p and kind in (1, 2, 3):
                for c in pending.pop(p, []):
                    if kind == 2 and p not in c.setdefault("undone_paths", []):
                        c["undone"] = True
                        c["undone_paths"].append(p)
        text = _message(r.get("message"))
        if text.strip() and not r.get("isSystemInitiated"):
            pr = base.prompt(t0, local, "copilot", text, session, f"{rel}:{r['_first_line']}" if log else f"{rel}#{i}")
            if gone:
                pr["retracted"] = True
            out["prompts"].append(pr)
            info["prompts"] += 1
            _touch(info, t0)

        rounds = [x for x in md.get("toolCallRounds") or [] if isinstance(x, dict)]
        done = st(_ms(_d(r.get("modelState")).get("completedAt")))
        until = done if done is not None else next((s for s in starts_of[i + 1:] if s is not None), None)
        stamped = [st(_ms(x.get("timestamp"))) for x in rounds]
        starts, prev = [], t0
        for s in stamped:                        # a round with no stamp starts no earlier than the one before
            prev = s if s is not None else prev
            starts.append(prev)
        end_of = lambda k, stamped=stamped, done=done: next((s for s in stamped[k + 1:] if s is not None), done)
        calls = [(k, c) for k, x in enumerate(rounds) for c in x.get("toolCalls") or [] if isinstance(c, dict)]
        parts = r.get("response") if isinstance(r.get("response"), list) else []
        got = _match(parts, calls, ctx["hooked"])
        cut = _cut(parts, sbr.get("afterUndoStop")) if sbr else len(parts)
        spawns = {p.get("toolCallId"): p for p in parts if _d(p).get("kind") == "toolInvocationSerialized"
                  and _d(p.get("toolSpecificData")).get("kind") == "subagent"}
        buf: list[tuple[int, dict]] = []
        edits: list[dict] = []
        last_edit, t_cur = None, t0

        # this request's state, bound now: both are called only within this iteration
        def flush(t, *, buf=buf, where=where, session=session, gone=gone, cut=cut, info=info) -> None:
            if buf:
                e = base.agent_text(t, "copilot", "copilot", "".join(_md_text(p) for _, p in buf),
                                    where(buf[0][1], buf[0][0]), session)
                if gone or any(j >= cut for j, _ in buf):
                    e["retracted"] = True
                out["events"].append(e)
                _touch(info, t)
                buf.clear()

        def edit_call(c: dict, model, *, edits=edits) -> None:
            c["_applied"], c["_model"], c["_whole"] = {}, model, {}
            edits.append(c)

        for j, p in enumerate(parts):
            if not isinstance(p, dict):
                continue
            kind = p.get("kind")
            if _is_md(p) and kind != "markdownVuln":
                if not FENCE.fullmatch(_md_text(p)):     # the fences edit tools print around an edit
                    buf.append((j, p))
                continue
            if kind == "toolInvocationSerialized":
                n = got.get(j)
                k, mc = calls[n] if n is not None else (None, None)
                tsd = _d(p.get("toolSpecificData"))
                name = mc.get("name") if mc else MODEL_NAME.get(p.get("toolId"), p.get("toolId") or "?")
                inp = _args(mc) if mc else _part_input(p)
                fl = _fields(name, inp)
                t = starts[k] if k is not None else t_cur
                t_res = end_of(k) if k is not None else until
                span = (t0, until)
                actor, sub = "copilot", p.get("subAgentInvocationId")
                if sub:
                    spawn = spawns.get(sub) or (next(iter(spawns.values())) if len(spawns) == 1 else {})
                    sd = _d(_d(spawn).get("toolSpecificData"))
                    actor = f"subagent:{sd.get('agentName') or '?'}"
                    begun = _ms(sd.get("startedAt"))
                    if begun is not None:              # its rounds are not in this request: its span is
                        t = st(begun)
                        t_res = st(begun + sd["duration"] / 1000.0) if isinstance(sd.get("duration"), (int, float)) \
                            else until
                        span = (t, t_res)
                    s = subagents.get(sub)
                    if s is None:
                        s = subagents[sub] = base.session_info("copilot", session, rel, actor)
                        s.update(type=sd.get("agentName") or "?", description=sd.get("description"),
                                 spawn_ref=where(_d(spawn), None) if spawn else None)
                        if isinstance(sd.get("modelName"), str):
                            s["models"].add(sd["modelName"])
                    s["calls"] += 1
                    _touch(s, t)
                command = fl.get("command")
                cwd_ = error = exit_code = None
                background = term_time = False
                if tsd.get("kind") == "terminal":
                    fl["op"] = base.COMMAND
                    cl = _d(tsd.get("commandLine"))
                    # what ran: runInTerminalTool.ts, userEdited ?? toolEdited ?? original
                    command = next((v for v in (cl.get("userEdited"), cl.get("toolEdited"), cl.get("original"))
                                    if isinstance(v, str)), tsd.get("command") if isinstance(tsd.get("command"), str)
                                   else command)
                    cwd_ = _path(tsd.get("cwd"))
                    state_ = _d(tsd.get("terminalCommandState"))
                    ran = _ms(state_.get("timestamp"))
                    if ran is not None:
                        t, term_time = st(ran), True
                        if isinstance(state_.get("duration"), (int, float)):
                            t_res = st(ran + state_["duration"] / 1000.0)
                    if isinstance(state_.get("exitCode"), int):
                        exit_code = state_["exitCode"]
                        error = f"exit {exit_code}" if exit_code != 0 else None     # it ran: still applied
                    background = bool(tsd.get("isBackground")) or _d(inp).get("mode") == "async"
                flush(t)
                op = fl["op"]
                c = base.tool_call(t, "copilot", actor, "", where(p, j), name=name, input=inp, op=op, session=session,
                                   id=mc.get("id") if mc else p.get("toolCallId"), command=command if op == base.COMMAND
                                   else None, applied=not _denied(p.get("isConfirmed")), t_result=t_res, error=error,
                                   background=background, cwd=cwd_)
                c["text"] = _summary(name, inp, fl.get("writes"), c["command"])
                c["_local"], c["_term_time"] = True, term_time
                if mc is None:
                    c["_pending"] = (name, span[0], span[1])
                if exit_code is not None:
                    c["exit_code"] = exit_code
                if gone or j >= cut:
                    c["retracted"] = True
                if op == base.EDIT:
                    edit_call(c, fl.get("writes"))
                    last_edit = c
                out["events"].append(c)
                info["calls"] += 1
                _touch(info, t)
                t_cur = max(x for x in (t_cur, t) if x is not None) if t is not None or t_cur is not None else None
            elif kind in ("textEditGroup", "notebookEditGroup"):
                path = _path(p.get("uri"))
                if not path:
                    continue
                if last_edit is None:            # edits with no tool call: edit mode, a code block applied
                    last_edit = base.tool_call(t_cur, "copilot", "copilot", path, where(p, j), name=kind,
                                               input={"uri": path}, op=base.EDIT, session=session)
                    edit_call(last_edit, None)
                    out["events"].append(last_edit)
                    info["calls"] += 1
                    _touch(info, t_cur)
                last_edit["_applied"].setdefault(path, []).extend(_edit_texts(p.get("edits")))
                last_edit["_whole"][path] = timeline.get((path, r.get("requestId")), [])
                if gone or j >= cut:
                    last_edit["retracted"] = True
            elif kind == "inlineReference" and buf:  # a file pill inside the text
                ref = p.get("inlineReference")
                label = p.get("name") or _d(ref).get("name") or os.path.basename(_path(_d(ref).get("uri", ref)) or "")
                buf.append((j, {"value": str(label or "")}))
        taken = set(got.values())
        for n, (k, mc) in enumerate(calls):      # the model called it, and no part shows it: a hook may
            if n in taken:
                continue
            name, inp = mc.get("name") or "?", _args(mc)
            fl = _fields(name, inp)
            c = base.tool_call(starts[k], "copilot", "copilot", _summary(name, inp, None, fl.get("command")),
                               f"{rel}:{r['_first_line']}" if log else f"{rel}#{i}", name=name, input=inp,
                               op=fl["op"], session=session, id=mc.get("id"), command=fl.get("command"),
                               t_result=end_of(k))
            c.update(_local=True, _term_time=False, _orphan=True, _info=info)
            if gone:
                c["retracted"] = True
            if c["op"] == base.EDIT:
                edit_call(c, fl.get("writes"))
            out["events"].append(c)
        tail = starts[-1] if starts else (done if done is not None else t0)
        flush(max(x for x in (tail, t_cur) if x is not None) if tail is not None or t_cur is not None else None)
        for c in edits:
            for p in c["_applied"]:
                pending[p].append(c)
    if not infos:
        info_of(cur or sid)
    for x in infos.values():
        x["models"] = sorted(x["models"])
    for s in subagents.values():
        s["models"] = sorted(s["models"])
        out["subagents"].append(s)
    return list(infos.values())


def _resolve(calls: list[dict], uses: dict, results: dict) -> tuple[int, set]:
    """Pairs what the chat file could not name with the hook log, then takes
    the hook's times. Returns how many calls stay without the model's input or
    a hook, and the ids of orphans no hook saw (dropped)."""
    claimed = {c["id"] for c in calls if c.get("id") and not c.get("_pending") and not c.get("_orphan")}
    drop = set()
    for c in calls:
        if not c.get("_orphan"):
            continue
        if c.get("id") in uses and c["id"] not in claimed:
            claimed.add(c["id"])
            c["_info"]["calls"] += 1
        else:
            drop.add(id(c))
    by_name: dict[str, list] = collections.defaultdict(list)
    for h in sorted((h for h in uses.values() if h.get("t") is not None), key=lambda h: h["t"]):
        by_name[h.get("tool_name")].append(h)
    lost = 0
    for c in sorted((c for c in calls if c.get("_pending")), key=lambda c: (c["_pending"][1] is None, c["_pending"][1] or 0)):
        name, lo, hi = c["_pending"]
        h = next((h for h in by_name.get(name, []) if h["tool_use_id"] not in claimed
                  and (lo is None or h["t"] >= lo - SLACK) and (hi is None or h["t"] <= hi + SLACK)), None)
        if h is None:
            lost += 1
            continue
        claimed.add(h["tool_use_id"])
        inp = h.get("tool_input")
        fl = _fields(name, inp)
        c["id"] = h["tool_use_id"]
        if inp is not None and not c["input"]:
            c["input"] = inp
        if c["op"] == base.EDIT:                 # merged with other text, so the cut line goes here
            c["_model"] = {p: _unclip(t) for p, t in (fl.get("writes") or {}).items()}
        elif c["op"] == base.COMMAND and not c["command"]:
            c["command"] = fl.get("command")
        vals = inp.values() if isinstance(inp, dict) else [inp]
        c["clipped"] = any(isinstance(v, str) and CLIP.search(v) for v in vals)
        c["t"] = h["t"]
        res = results.get(h["tool_use_id"])
        if res is not None and res.get("t") is not None:
            c["t_result"] = res["t"]
    for c in calls:                              # a round spans a model response; the hook saw the call itself
        h = uses.get(c.get("id"))
        if not c.get("_local") or c.get("_pending") or c.get("_term_time") or h is None or h.get("t") is None:
            continue
        c["t"] = h["t"]
        res = results.get(c["id"])
        if res is not None and res.get("t") is not None:
            c["t_result"] = res["t"]
    return lost, drop


# ---------------------------------------------------------------- the Copilot harness

def _yaml_cwd(p: pathlib.Path) -> str | None:
    """workspace.yaml's cwd line; the file holds a flat mapping."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^cwd:[ \t]*(.+?)[ \t]*$", text, re.M)
    if not m:
        return None
    v = m.group(1)
    if v[:1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v.strip('"')
    return v[1:-1].replace("''", "'") if v[:1] == "'" else v


def _start_cwd(events: pathlib.Path) -> str | None:
    try:
        with events.open(encoding="utf-8", errors="replace") as fh:
            head = json.loads(fh.readline())
    except (OSError, ValueError):
        return None
    if _d(head).get("type") != "session.start":
        return None
    cwd = _d(_d(head.get("data")).get("context")).get("cwd")
    return cwd if isinstance(cwd, str) else None


def _cli(tr: dict, f: pathlib.Path, rel: str, bad, out: dict, ctx: dict):
    """One Copilot harness or Copilot CLI session: its events.jsonl. The SDK
    refuses a log whose first event is not session.start, and so does this."""
    from .. import trace
    rows = list(trace.rows(f, bad, rel))
    if not rows:
        return None
    head = rows[0][1]
    if head.get("type") != "session.start":
        return {"format": f"the log starts with {head.get('type')!r}, not session.start"}
    d0 = _d(head.get("data"))
    if d0.get("version") not in (None, 1):
        return {"format": f"session.start version {d0.get('version')!r}, not 1"}
    sid = d0["sessionId"] if isinstance(d0.get("sessionId"), str) else f.parent.name
    cwd = _yaml_cwd(f.parent / "workspace.yaml") or _d(d0.get("context")).get("cwd")
    info = base.session_info("copilot", sid, rel, "copilot", cwd=cwd if isinstance(cwd, str) else None)
    info["chat_session"] = sid
    if isinstance(d0.get("copilotVersion"), str):
        ctx["versions"].add(f"copilot-cli {d0['copilotVersion']}")
    calls: dict[str, dict] = {}
    parents: dict[str, str] = {}
    tasks: dict[str, dict] = {}

    def owner(cid) -> str | None:
        """The task (subagent) call a call runs under, walking parentToolCallId."""
        p, hops = parents.get(cid), 0
        while p and p not in tasks and hops < 50:
            p, hops = parents.get(p), hops + 1
        return p if p in tasks else None

    for n, r in rows:
        typ, d = r.get("type"), _d(r.get("data"))
        local = trace.epoch(r.get("timestamp"))
        t = trace.server_time(tr, local)
        ref = f"{rel}:{n}"
        if typ in ("session.start", "session.resume") and isinstance(d.get("selectedModel"), str):
            info["models"].add(d["selectedModel"])
        elif typ == "session.model_change" and isinstance(d.get("newModel"), str):
            info["models"].add(d["newModel"])
        elif typ == "user.message":
            text, src = d.get("content"), d.get("source")
            if isinstance(text, str) and text.strip() and (not src or str(src).lower() == "user"):
                out["prompts"].append(base.prompt(t, local, "copilot", text, sid, ref))
                info["prompts"] += 1
                _touch(info, t)
        elif typ == "assistant.message":
            text = d.get("content")
            if isinstance(text, str) and text.strip():
                own = tasks.get(owner(d.get("parentToolCallId")) or d.get("parentToolCallId") or "")
                out["events"].append(base.agent_text(t, "copilot", own["actor"] if own else "copilot", text, ref, sid))
                _touch(info, t)
        elif typ in ("tool.execution_start", "tool.user_requested"):
            cid = d.get("toolCallId")
            name = d.get("toolName") if isinstance(d.get("toolName"), str) else "?"
            inp = d.get("arguments")
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except ValueError:
                    pass                        # kept as the text it is
            if isinstance(d.get("parentToolCallId"), str):
                parents[cid] = d["parentToolCallId"]
            fl = _fields(name, inp)
            task = tasks.get(owner(cid) or "")
            # tool.user_requested is the candidate's own `!command`
            actor = "candidate" if typ == "tool.user_requested" else task["actor"] if task else "copilot"
            c = base.tool_call(t, "copilot", actor, _summary(name, inp, fl.get("writes"), fl.get("command")), ref,
                               name=name, input=inp, op=fl["op"], session=sid, id=cid, writes=fl.get("writes"),
                               command=fl.get("command"), cwd=info["cwd"] if fl["op"] == base.COMMAND else None)
            if isinstance(cid, str):
                calls[cid] = c
            out["events"].append(c)
            info["calls"] += 1
            _touch(info, t)
            if task:
                task["calls"] += 1
                _touch(task, t)
            if name == "task" and isinstance(cid, str):
                typ_ = _d(inp).get("agent_type") or "?"
                s = tasks[cid] = base.session_info("copilot", sid, rel, f"subagent:{typ_}")
                s.update(type=typ_, description=_d(inp).get("description"), spawn_ref=ref)
        elif typ == "tool.execution_complete":
            c = calls.get(d.get("toolCallId"))
            if c is None:
                continue
            c["t_result"] = t
            if d.get("isUserRequested"):
                c["actor"] = "candidate"
            if d.get("success") is False:
                c["error"] = True
                # a failed edit changed nothing; a patch may have written some files first
                if c["op"] == base.EDIT and ALIASES.get(c["name"], c["name"]) != "apply_patch":
                    c["applied"] = False
    info["models"] = sorted(info["models"])
    for s in tasks.values():
        s["models"] = sorted(s["models"])
        out["subagents"].append(s)
    return [info]


# ---------------------------------------------------------------- settings

def _pick(s: dict) -> dict:
    """SETTINGS only, at the top and inside [language] blocks."""
    out = {k: s[k] for k in SETTINGS if k in s}
    for k, v in s.items():
        if isinstance(k, str) and k.startswith("[") and isinstance(v, dict):
            blk = {kk: v[kk] for kk in SETTINGS if kk in v}
            if blk:
                out[k] = blk
    return out


def _read_settings(p: pathlib.Path) -> dict | None:
    try:
        return _pick(_d(_jsonc(p.read_text(encoding="utf-8", errors="replace"))))
    except (OSError, ValueError):
        return None


def _layers(saved) -> dict:
    """{user|profile|workspace: settings}; older kits saved the user's flat."""
    if not isinstance(saved, dict):
        return {}
    if any(k in saved for k in ("user", "profile", "workspace")):
        return {k: _d(saved.get(k)) for k in ("user", "profile", "workspace") if k in saved}
    return {"user": saved}


def _scopes(layers: dict) -> list[dict]:
    """Most specific first. A profile with its own settings replaces the user's."""
    return [_d(layers.get("workspace")), _d(layers["profile"] if "profile" in layers else layers.get("user"))]


def _setting(layers: dict, key: str, default):
    return next((s[key] for s in _scopes(layers) if key in s), default)


def _ai_off(layers: dict) -> bool:
    return _setting(layers, "chat.disableAIFeatures", False) is True


def _completions_on(layers: dict) -> bool:
    """Whether inline suggestions or next-edit suggestions could have written
    code in any language. Every key defaults to on (the built-in extension's
    package.json); a language block that turns one on counts; an object
    setting merges over the defaults, as VS Code merges it."""
    langs = [v for s in _scopes(layers) for k, v in s.items() if str(k).startswith("[") and isinstance(v, dict)]

    def on(key: str) -> bool:
        return _setting(layers, key, True) is not False or any(b.get(key) is True for b in langs)
    en: object = dict(ENABLE_DEFAULT)
    for s in reversed(_scopes(layers)):
        v = s.get("github.copilot.enable")
        if isinstance(v, dict) and isinstance(en, dict):
            en = {**en, **v}
        elif v is not None:
            en = v
    ghost = any(x is not False for x in en.values()) if isinstance(en, dict) else en is not False
    ghost = ghost or any(b.get("github.copilot.enable") not in (None, False) for b in langs)
    return on("editor.inlineSuggest.enabled") and (ghost or on("github.copilot.nextEditSuggestions.enabled"))


# ---------------------------------------------------------------- collect helpers

def _state_keys(db: pathlib.Path) -> dict:
    """STATE_KEYS from a copy of a state.vscdb, write-ahead log included: VS
    Code holds the file open, and its newest rows may be in -wal only. Of the
    index, each entry's non-descriptive fields only: no titles."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ivkit-vscdb-"))
    try:
        for side in ("", "-wal", "-shm"):
            src = db.parent / (db.name + side)
            if src.is_file():
                shutil.copy2(src, tmp / src.name)
        con = sqlite3.connect(str(tmp / db.name))
        try:
            out = {}
            for k in STATE_KEYS:
                row = con.execute("select value from ItemTable where key = ?", (k,)).fetchone()
                if row is None:
                    continue
                v = row[0].decode("utf-8", "replace") if isinstance(row[0], bytes) else row[0]
                try:
                    v = json.loads(v)
                except (TypeError, ValueError):
                    continue
                if isinstance(v, dict):
                    out[k] = {"version": v.get("version"), "entries": {
                        str(ek): {f: e[f] for f in INDEX_FIELDS if f in e}
                        for ek, e in _d(v.get("entries")).items() if isinstance(e, dict)}}
            return out
        finally:
            con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _round_calls(r: dict) -> list[dict]:
    return [c for x in _d(_d(r.get("result")).get("metadata")).get("toolCallRounds") or [] if isinstance(x, dict)
            for c in x.get("toolCalls") or [] if isinstance(c, dict)]


def _named(c: dict) -> list:
    """The paths a round call names: what it writes, and its path arguments."""
    args = _args(c)
    d = _d(args)
    return list(_fields(c.get("name") or "", args).get("writes") or {}) + \
        [v for k in ("filePath", "path", "file_path", "dirPath", "cwd") for v in [d.get(k)] if isinstance(v, str)]


def _touched(r: dict, repo: pathlib.Path) -> bool:
    """Whether a request worked in the repository: an edit of a file in it, a
    command run in it, or a call naming a path in it. file: URIs and absolute
    paths only."""
    for p in r.get("response") if isinstance(r.get("response"), list) else []:
        p = _d(p)
        if p.get("kind") in EDIT_KINDS and _under(_path(p.get("uri")), repo):
            return True
        if p.get("kind") == "workspaceEdit" and isinstance(p.get("edits"), list) and any(
                _under(_path(_d(e).get(k)), repo) for e in p["edits"] for k in ("oldResource", "newResource")):
            return True
        tsd = _d(p.get("toolSpecificData"))
        if tsd.get("kind") == "terminal" and _under(_path(tsd.get("cwd")), repo):
            return True
    return any(_under(x, repo) for c in _round_calls(r) for x in _named(c))


def _clean(r: dict) -> dict:
    """A request as the log held it, without replay's stamps."""
    out = {k: v for k, v in r.items() if k not in ("_line", "_removed", "_first_line")}
    if isinstance(out.get("response"), list):
        out["response"] = [{k: v for k, v in p.items() if k != "_line"} if isinstance(p, dict) else p
                           for p in out["response"]]
    return out


def _redact(r: dict, repo: pathlib.Path) -> dict:
    """A request of another folder's chat as the parser reads it, and no more:
    no attachments, rendered prompt, tool output or summary, and no edit or
    edit call of a file elsewhere, whose text is that file's."""
    here = lambda u: _under(_path(u), repo)
    out = {k: r[k] for k in REQUEST_KEEP if k in r}
    out["message"] = {"text": _message(r.get("message"))}
    md = _d(_d(r.get("result")).get("metadata"))
    gone, rounds = set(), []                     # gone: base ids of the edit calls that wrote only elsewhere
    for x in md.get("toolCallRounds") or []:
        if not isinstance(x, dict):
            continue
        calls = []
        for c in x.get("toolCalls") or []:
            if not isinstance(c, dict):
                continue
            w = _fields(c.get("name") or "", _args(c)).get("writes") or {}
            if w and not any(_under(p, repo) for p in w):
                gone.add(str(c.get("id") or "").split("__vscode-")[0])
                continue
            calls.append({k: c[k] for k in ("name", "id", "arguments") if k in c})
        rounds.append({**{k: x[k] for k in ("id", "timestamp") if k in x}, "toolCalls": calls})
    if "result" in r:
        out["result"] = {"metadata": {**({"sessionId": md["sessionId"]} if "sessionId" in md else {}),
                                      "toolCallRounds": rounds}}
    parts = r.get("response") if isinstance(r.get("response"), list) else []
    kept = []
    for j, p in enumerate(parts):
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if kind in EDIT_KINDS:
            if here(p.get("uri")):
                kept.append({k: v for k, v in p.items() if k != "_line"})
        elif kind == "toolInvocationSerialized":
            hint = _hint(parts, j)
            if p.get("toolCallId") in gone or (hint and hint[0] == "paths" and not any(_under(x, repo) for x in hint[1])):
                continue                         # a call that edited only files elsewhere
            q = {k: p[k] for k in TOOL_KEEP if k in p}
            if "toolSpecificData" in q:
                q["toolSpecificData"] = {k: v for k, v in _d(p["toolSpecificData"]).items() if k in TOOL_DATA_KEEP}
            if "input" in _d(p.get("resultDetails")):
                q["resultDetails"] = {"input": p["resultDetails"]["input"]}
            kept.append(q)
        elif _is_md(p) and kind != "markdownVuln":
            kept.append({"value": _md_text(p)})
        elif kind == "undoStop":
            kept.append({"kind": kind, "id": p.get("id")})
        elif kind == "inlineReference":
            ref = p.get("inlineReference")
            kept.append({"kind": kind, "name": p.get("name") or _d(ref).get("name")
                         or os.path.basename(_path(_d(ref).get("uri", ref)) or "")})
    out["response"] = kept
    if isinstance(r.get("editedFileEvents"), list):
        out["editedFileEvents"] = [e for e in r["editedFileEvents"] if here(_d(e).get("uri"))]
    return out


def _reduce(f: pathlib.Path, repo: pathlib.Path, whole: bool, cutoff: float | None, tmp: pathlib.Path):
    """What of a chat file leaves: the file itself, a copy written under tmp,
    a format warning ({size, mtime, format}), or None. A request from before
    cutoff (this machine's clock) never leaves. A chat of a window on this
    folder alone (whole), or one whose working directory is here, keeps the
    rest as it was; any other keeps only the requests that worked here,
    through _redact. A .jsonl is written back as a mutation log whose replay
    gives the kept requests, one a restore cut away still cut away. A chat
    this cannot read is never copied, since nothing of it could be cut: of a
    whole one only the warning leaves, for the parser to say so."""
    bad: collections.Counter = collections.Counter()
    log = f.suffix == ".jsonl"
    state, reqs, fmt = replay(f, bad, f.name) if log else _flat(f, bad, f.name)
    if state is None:
        if not whole or (fmt == "ok" and not f.read_bytes().strip()):
            return None                         # another window's chat, or a file holding nothing
        st = f.stat()
        what = fmt[len(UNRECOGNISED):] if fmt != "ok" else "nothing in it could be read"
        return {"size": st.st_size, "mtime": st.st_mtime, "format": what[:80]}   # a value it names may be anything
    whole = whole or _under(_path(state.get("workingDirectory")), repo)
    old = lambda r: cutoff is not None and (_ms(r.get("timestamp")) or cutoff) < cutoff
    keep = [r for r in reqs if not old(r) and (whole or _touched(r, repo))]
    if whole and len(keep) == len(reqs):
        return f
    if not keep:
        return None
    top = {k: v for k, v in state.items() if k != "requests"} if whole else \
        {k: state[k] for k in CHAT_KEEP if k in state}
    clean = [_clean(r) if whole else _redact(r, repo) for r in keep]
    p = pathlib.Path(tempfile.mkdtemp(dir=tmp)) / f.name
    if not log:
        p.write_text(json.dumps({**top, "requests": clean}, ensure_ascii=False), encoding="utf-8")
        return p
    lines, finals, tail = [{"kind": 0, "v": {**top, "requests": []}}], 0, False
    for r, c in zip(keep, clean):
        e = {"kind": 2, "k": ["requests"], "v": [c]}
        if r.get("_removed"):
            tail = True
        else:
            if tail:
                e["i"] = finals                  # cuts the removed ones before it away, as the restore did
            finals, tail = finals + 1, False
        lines.append(e)
    if tail:
        lines.append({"kind": 2, "k": ["requests"], "i": finals})
    p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines), encoding="utf-8")
    return p


def _reduce_cli(ev: pathlib.Path, cutoff: float | None, tmp: pathlib.Path):
    """What of a Copilot CLI session's events.jsonl leaves: the file itself,
    a copy written under tmp, a format warning, or None. A resumed session
    holds its earlier runs: no record from before cutoff (this machine's
    clock) leaves, and one with no time of its own, or no JSON, goes with the
    record before it. The first, session.start, stays: the parser needs it."""
    if cutoff is None:
        return ev
    with open(ev, "rb") as fh:
        lines = [x for x in fh if x.strip()]
    keep, t, dated, head = [], None, False, None
    for n, raw in enumerate(lines):
        try:
            r = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            r = None
        own = _epoch(_d(r).get("timestamp"))
        head = n if head is None and isinstance(r, dict) else head
        dated = dated or (own is not None and n != head)
        t = own if own is not None else t
        if (n == head and r.get("type") == "session.start") or (t is not None and t >= cutoff):
            keep.append(n)
    if len(keep) == len(lines):
        return ev
    if not dated:
        st = ev.stat()
        return {"size": st.st_size, "mtime": st.st_mtime, "format": "no record after the first has a time"}
    if all(n == head for n in keep):
        return None                             # nothing of it from the session
    p = pathlib.Path(tempfile.mkdtemp(dir=tmp)) / ev.name
    p.write_bytes(b"".join(lines[n] for n in keep))
    return p


def _editing(state, repo: pathlib.Path) -> tuple[dict, set]:
    """A chat's editing session (state.json) with only the files here: their
    baselines, operations and snapshot entries, and the contents/ hashes
    those name. Other files' text stays behind."""
    here = lambda u: _under(_path(u), repo)
    s = _d(state)
    lst = lambda v: v if isinstance(v, list) else []
    init = [x for x in lst(s.get("initialFileContents")) if isinstance(x, list) and len(x) == 2 and here(x[0])]
    out = {k: s[k] for k in ("version",) if k in s}
    out["initialFileContents"] = init
    hashes = {x[1] for x in init if isinstance(x[1], str)}
    if isinstance(s.get("recentSnapshot"), dict):
        snap = dict(s["recentSnapshot"])
        snap["entries"] = [e for e in lst(snap.get("entries")) if isinstance(e, dict) and here(e.get("resource"))]
        if "workingSet" in snap:
            snap["workingSet"] = [x for x in lst(snap["workingSet"]) if isinstance(x, list) and x and here(x[0])]
        hashes |= {e[k] for e in snap["entries"] for k in ("originalHash", "currentHash") if isinstance(e.get(k), str)}
        out["recentSnapshot"] = snap
    tl = s.get("timeline")
    if isinstance(tl, dict):
        uri = lambda b: _d(b[1] if isinstance(b, list) and len(b) == 2 else b).get("uri")
        out["timeline"] = {**{k: tl[k] for k in ("checkpoints", "currentEpoch", "epochCounter") if k in tl},
                           "fileBaselines": [b for b in lst(tl.get("fileBaselines")) if here(uri(b))],
                           "operations": [o for o in lst(tl.get("operations"))
                                          if any(here(_d(o).get(k)) for k in ("uri", "oldUri", "newUri"))]}
    return out, hashes


def _offer(chats: dict, f: pathlib.Path, sub: str, data) -> None:
    """The same chat is copied, not moved, when a window changes workspace:
    keep the newest copy. data is what of it leaves (_reduce)."""
    try:
        m = f.stat().st_mtime
    except OSError:
        return
    key = (f.stem, f.suffix)
    if key not in chats or m > chats[key][0]:
        chats[key] = (m, f.name, sub, data)


def _started(repo: pathlib.Path) -> float | None:
    """When the session began, on this machine's clock."""
    v = _d(_load(repo / ".interview" / "recorder.json")).get("started")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _since(repo: pathlib.Path) -> float | None:
    """The session start less SINCE_SLACK: a store last written before it
    holds another project at the same path."""
    v = _started(repo)
    return v - SINCE_SLACK if v is not None else None


def _log_activity(homes: list, since: float | None) -> list | None:
    """[first, last] modification time (this machine's clock) of the Copilot
    extension's logs written since the session began, or None. Only their
    times: a log's content is the user's, from every window."""
    times, seen = [], set()
    for home in homes:
        for pat in EXT_LOGS:
            for f in home.glob(pat):
                try:
                    key, m = f.resolve(), f.stat().st_mtime
                except OSError:
                    continue
                if key not in seen and f.is_file() and (since is None or m >= since - SLACK):
                    seen.add(key)
                    times.append(m)
    return [min(times), max(times)] if times else None


def _fresh(p: pathlib.Path, since: float | None) -> bool:
    if since is None:
        return True
    try:
        return p.stat().st_mtime >= since
    except OSError:
        return False


def _profile_settings(user: pathlib.Path, repo: pathlib.Path) -> dict | None:
    """The settings of the profile VS Code opens this folder with, when it is
    not the default one and has its own."""
    assoc = _d(_d(_d(_load(user / "globalStorage" / "storage.json")).get("profileAssociations")).get("workspaces"))
    for k, pid in assoc.items():
        if isinstance(pid, str) and pid != DEFAULT_PROFILE and _relation([_path(k) or ""], repo) == "full":
            if re.fullmatch(r"[\w.-]+", pid):
                return _read_settings(user / "profiles" / pid / "settings.json")
    return None


def _evidence(tr: dict, logs: pathlib.Path, kept: list) -> tuple[list[str], list[float]]:
    """What shows Copilot was usable in the session, and when it was used: a
    chat request in it (Local or Copilot harness), a Copilot hook record in
    it, or the extension's log written in it. Only the first two are use, and
    only they give times: every VS Code window writes the log. The session
    runs from its start to its deadline; either end unknown is open."""
    from .. import trace
    s = _d(tr.get("session"))
    lo = s["start"] - SLACK if isinstance(s.get("start"), (int, float)) else None
    hi = s["deadline"] + SLACK if isinstance(s.get("deadline"), (int, float)) else None
    inside = lambda t: isinstance(t, (int, float)) and (lo is None or t >= lo) and (hi is None or t <= hi)
    out, times = [], []
    got = [x["t"] for buf, _ in kept for x in buf["prompts"] + buf["events"] if inside(x.get("t"))]
    if got:
        out.append("chat request")
        times += got
    hooks = [h for h in tr.get("hooks") or [] if h.get("tool") == "copilot" and (h.get("t") is None or inside(h["t"]))]
    if hooks:
        out.append("hook records")
        times += [h["t"] for h in hooks if h.get("t") is not None]
    act = _d(_load(logs / LOG_ACTIVITY)).get("copilot_log_activity")
    if isinstance(act, list) and len(act) == 2 and all(isinstance(x, (int, float)) for x in act):
        a, b = trace.server_time(tr, float(act[0])), trace.server_time(tr, float(act[1]))
        if (lo is None or b >= lo) and (hi is None or a <= hi):
            out.append("extension log")
    return out, times


def _hook_cmd(python: str, script: str, event: str) -> dict:
    """The command, quoted so that no path expands. It cannot fail: exit 2 is a
    blocking error to VS Code (hookExecutor.ts), which would abort every prompt
    and deny every tool, and on Stop tell the agent to keep going. On Windows
    VS Code runs it in Windows PowerShell."""
    ps = lambda v: "'" + v.replace("'", "''") + "'"
    return {"command": f"{shlex.quote(python)} {shlex.quote(script)} --tool copilot {event} || true",
            "windows": f"& {ps(python)} {ps(script)} --tool copilot {event}; exit 0"}


class Copilot(base.Adapter):
    name = "copilot"
    title = "GitHub Copilot (VS Code)"

    def detect(self) -> str | None:
        v = _d(_load(APP_PACKAGE)).get("version")
        if isinstance(v, str) and v:
            return v
        exe = shutil.which("code")
        if not exe:
            return None
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return "installed"
        m = VERSION.search(r.stdout or "") if r.returncode == 0 else None
        return m.group(0) if m else "installed"

    def hook_files(self, python: str, hook_script: str, repo_root: str | None = None) -> dict[str, str]:
        """PascalCase events, which VS Code and the Copilot CLI both read, and
        `version: 1`, which the CLI needs and VS Code ignores. Hooks run in the
        workspace root, so the build-time default names the script relative
        to it."""
        doc = {"version": 1, "hooks": {ev: [{"type": "command", **_hook_cmd(python, hook_script, name), "timeout": 5}]
                                       for ev, name in HOOKS}}
        return {HOOK_FILE: json.dumps(doc, indent=2) + "\n"}

    def mcp_files(self, python: str, mcp_script: str, repo_root: str | None = None) -> dict[str, str]:
        from ..configs import SERVER
        script, repo = (mcp_script, repo_root) if repo_root is not None else \
            ("${workspaceFolder}/" + mcp_script, "${workspaceFolder}")
        doc = {"servers": {SERVER: {"type": "stdio", "command": python, "args": [script],
                                    "env": {"INTERVIEW_REPO": repo}}}}
        return {".vscode/mcp.json": json.dumps(doc, indent=2) + "\n"}

    def collect(self, repo: pathlib.Path, out: pathlib.Path, save) -> tuple[list[str], list[str]]:
        """This repository's workspace, matched on workspace.json: a window on
        this folder alone. From any other window on it (a folder around it or
        inside it, a multi-root workspace) or with no folder, only the requests
        that worked here, and only what the parser reads of them. Copilot CLI
        sessions whose working directory is here. Nothing last written before
        the session began, no request or CLI record from before it, and of an
        editing session only the files here. Of the user's settings only
        SETTINGS, and of the state database only the chat index. Of a file in
        a format this cannot read, only collect.json's note of it."""
        repo = pathlib.Path(repo).resolve()
        since = _since(repo)
        cutoff = _started(repo)
        cutoff = cutoff - REQUEST_SLACK if cutoff is not None else None
        n: collections.Counter = collections.Counter()
        found, skipped = [], []
        chats: dict = {}
        unread: list[dict] = []                  # what collect could not read: that it was there, and nothing of it
        user_seen = None
        homes = [d for d in user_data_dirs() if (d / "User").is_dir()]
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="ivkit-copilot-"))

        def take(f: pathlib.Path, whole: bool):
            try:
                return _reduce(f, repo, whole, cutoff, tmp)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as e:
                skipped.append(f"{self.title}: could not read {f.name} ({type(e).__name__}), so it was left out")
                return None
        try:
            for home in homes:
                user = home / "User"
                for wd in sorted((user / "workspaceStorage").glob("*")):
                    rel = _relation(_folders(wd / "workspace.json"), repo) if wd.is_dir() else None
                    files = [x for x in sorted((wd / "chatSessions").glob("*"))
                             if x.is_file() and x.suffix in (".json", ".jsonl")]
                    if rel is None or not any(_fresh(x, since) for x in [wd / "state.vscdb", wd / "state.vscdb-wal",
                                                                          wd / "workspace.json", *files]):
                        continue
                    mine = {x: d for x in files if _fresh(x, since) for d in [take(x, rel == "full")] if d is not None}
                    if rel == "part" and not mine:
                        continue
                    save(wd.name, "workspace.json", wd / "workspace.json")
                    user_seen = user_seen or user
                    for f, data in mine.items():
                        _offer(chats, f, f"{wd.name}/chatSessions", data)
                    for sd in sorted((wd / "chatEditingSessions").glob("*")):
                        if sd.name not in {x.stem for x, d in mine.items() if not isinstance(d, dict)} or \
                                not _fresh(sd / "state.json", since):
                            continue
                        state, hashes = _editing(_load(sd / "state.json"), repo)
                        sub = f"{wd.name}/chatEditingSessions/{sd.name}"
                        if not save(sub, "state.json", state):
                            continue
                        n["editing"] += 1
                        for h in sorted(hashes):
                            if re.fullmatch(r"[\w.-]+", h) and (sd / "contents" / h).is_file():
                                save(f"{sub}/contents", h, sd / "contents" / h)
                    if rel != "full":
                        continue
                    for f in sorted((wd / "GitHub.copilot-chat" / "transcripts").glob("*")):
                        if f.is_file() and _fresh(f, since) and \
                                save(f"{wd.name}/GitHub.copilot-chat/transcripts", f.name, f):
                            n["transcripts"] += 1
                    if (wd / "state.vscdb").is_file():
                        try:
                            keys = _state_keys(wd / "state.vscdb")
                        except (sqlite3.Error, OSError) as e:
                            skipped.append(f"{self.title}: could not read {wd.name}/state.vscdb ({type(e).__name__})")
                        else:
                            if keys:
                                save(wd.name, "state.json", keys)
                for d in (user / "globalStorage" / "emptyWindowChatSessions",
                          user / "workspaceStorage" / "no-workspace" / "chatSessions"):
                    for f in sorted(d.glob("*")):
                        if f.is_file() and f.suffix in (".json", ".jsonl") and _fresh(f, since):
                            data = take(f, False)
                            if data is not None:
                                _offer(chats, f, f"{EMPTY_DIR}/chatSessions", data)
                                user_seen = user_seen or user
            for _, name, sub, data in (chats[k] for k in sorted(chats)):
                if isinstance(data, dict):
                    unread.append({"file": f"{sub}/{name}", **data})
                elif save(sub, name, data):
                    n["chats"] += 1
            for d in sorted((copilot_home() / "session-state").glob("*")):
                ev = d / "events.jsonl"
                if not ev.is_file() or not _fresh(ev, since):
                    continue
                yaml, sub = d / "workspace.yaml", f"{CLI_DIR}/{d.name}"
                cwd = _yaml_cwd(yaml) if yaml.is_file() else _start_cwd(ev)
                if not _under(cwd, repo):
                    continue
                try:
                    data = _reduce_cli(ev, cutoff, tmp)
                except OSError as e:
                    skipped.append(f"{self.title}: could not read {sub}/events.jsonl ({type(e).__name__}), "
                                   "so it was left out")
                    continue
                if isinstance(data, dict):
                    unread.append({"file": f"{sub}/events.jsonl", **data})
                elif data is not None and save(sub, "events.jsonl", data):
                    n["cli"] += 1
                    if yaml.is_file() and data != ev:   # a resumed session: its summary is of the earlier run
                        yaml = data.parent / yaml.name
                        yaml.write_text(f"cwd: {json.dumps(cwd)}\n", encoding="utf-8")
                    if yaml.is_file():
                        save(sub, "workspace.yaml", yaml)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if unread:
            save("", "collect.json", {"unrecognised": unread})
        for u in unread:
            skipped.append(f"{self.title}: {u['file']} is in a format this collector does not know ({u['format']}), "
                           "so it was left out; if you used Copilot, export the chat yourself")
        if user_seen is not None:
            layers = {"user": _read_settings(user_seen / "settings.json") or {}}   # unreadable: the defaults, on
            prof = _profile_settings(user_seen, repo)
            if prof is not None:
                layers["profile"] = prof
            ws = _read_settings(repo / ".vscode" / "settings.json")
            if ws is not None:
                layers["workspace"] = ws
            save("", "settings.json", layers)
            save("", LOG_ACTIVITY, {"copilot_log_activity": _log_activity(homes, _started(repo))})
        bits = [f"{n[k]} {what}" for k, what in (("chats", "chat session(s)"), ("editing", "editing session(s)"),
                                                   ("transcripts", "hook transcript(s)"),
                                                   ("cli", "Copilot CLI session(s)")) if n[k]]
        if bits:
            found.append(f"{self.title}: " + ", ".join(bits))
        elif homes and user_seen is None:
            skipped.append(f"{self.title}: installed, but no workspace for this repository. "
                           "If you used it, export the chat yourself.")
        return found, skipped

    def parse(self, tr: dict, logs: pathlib.Path, bad) -> dict:
        out = base.parsed()
        root = logs.parent
        problems: list[str] = []
        tried = ok = 0
        uses, results = {}, {}
        for h in tr.get("hooks") or []:
            if h.get("tool") != self.name or not isinstance(h.get("tool_use_id"), str):
                continue
            if h.get("event") == "tool_use":
                uses.setdefault(h["tool_use_id"], h)
            elif h.get("event") == "tool_result":
                results.setdefault(h["tool_use_id"], h)
        ctx = {"hooked": uses, "versions": set()}
        dirs = sorted(p for p in logs.iterdir() if p.is_dir()) if logs.is_dir() else []
        chats: dict[str, tuple] = {}                 # chat id -> (file, its workspace's folder)
        for d in dirs:
            if d.name == CLI_DIR:
                continue
            cwd = _folder(d / "workspace.json")
            for f in sorted((d / "chatSessions").glob("*")):
                if not f.is_file() or f.suffix not in (".json", ".jsonl"):
                    continue
                have = chats.get(f.stem)
                # the .jsonl supersedes a .json of the same chat; else the fuller copy
                if have is None or (f.suffix, f.stat().st_size) > (have[0].suffix, have[0].stat().st_size):
                    chats[f.stem] = (f, cwd)
        jobs = [(f, _chat, (cwd,)) for f, cwd in (chats[k] for k in sorted(chats))]
        jobs += [(f, _cli, ()) for f in sorted((logs / CLI_DIR).glob("*/events.jsonl"))]
        kept = []
        for f, fn, extra in jobs:
            rel = str(f.relative_to(root))
            buf = {"events": [], "prompts": [], "subagents": []}   # a chat whose parser fails leaves nothing
            try:
                got = fn(tr, f, rel, *extra, bad, buf, ctx)
            except Exception as e:                   # that chat is lost, not the report; the format says so
                tr["caveats"].append(f"could not read {rel}: {type(e).__name__}: {str(e)[:200]}")
                problems.append(f"{rel}: the parser failed ({type(e).__name__})")
                tried += 1
                continue
            if got is None:
                continue                             # empty, or only torn lines: nothing to understand
            tried += 1
            if isinstance(got, dict):
                problems.append(f"{rel}: {got['format']}")
                continue
            kept.append((buf, got))
            ok += 1
        for w in _d(_load(logs / "collect.json")).get("unrecognised") or []:
            if isinstance(w, dict):                  # a file collect could not read, and so left behind
                problems.append(f"{logs.name}/{w.get('file')}: {w.get('format')}")
                tried += 1
        calls = [e for buf, _ in kept for e in buf["events"] if e.get("kind") == "tool_call"]
        lost, drop = _resolve(calls, uses, results)
        for c in calls:
            if "_applied" in c:
                c["writes"] = _merge(c["name"], c["_applied"], c["_model"], c["_whole"])
                if c["name"] in ("textEditGroup", "notebookEditGroup") or not c["text"]:
                    c["text"] = _summary(c["name"], c["input"], c["writes"], None)
            for k in PRIVATE:
                c.pop(k, None)
        for buf, infos in kept:
            out["events"] += [e for e in buf["events"] if id(e) not in drop]
            out["prompts"] += buf["prompts"]
            out["subagents"] += buf["subagents"]
            out["sessions"] += infos
        out["coverage"]["calls_without_input"] = lost
        if lost:
            tr["caveats"].append(f"{lost} Copilot call(s) (a subagent's, or in a request whose rounds were not "
                                 "stored) have neither the model's input nor a hook record: their edits are known "
                                 "only from the text VS Code applied (whole lines where the editing session was "
                                 "collected), and they are timed only to the subagent's or the request's span")
        external: collections.Counter = collections.Counter()
        for sj in sorted(logs.glob("*/state.json")):
            entries = _d(_d(_d(_load(sj)).get("chat.ChatSessionStore.index")).get("entries"))
            for k, e in entries.items():
                if _d(e).get("isExternal") and ":" in k:
                    external[k.split(":", 1)[0]] += 1
        if external:
            out["coverage"]["external_sessions"] = dict(external)
        cloud = sum(v for k, v in external.items() if "cloud" in k)
        if cloud:
            tr["caveats"].append(f"VS Code's chat index lists {cloud} Copilot cloud-agent session(s) here: they ran "
                                 "on GitHub and left no local record, so their work is not in this trace")
        saved = _load(logs / "settings.json")
        layers = _layers(saved)
        if "workspace" not in layers:                # the repository's own, as it was submitted
            ws = _read_settings(pathlib.Path(str(tr.get("repo") or "")) / ".vscode" / "settings.json")
            if ws is not None:
                layers["workspace"] = ws
        # VS Code alone says nothing: the Claude and Codex extensions live in it too
        evidence, used = _evidence(tr, logs, kept)
        out["coverage"]["copilot_evidence"] = evidence
        vscode = any(d.name != CLI_DIR for d in dirs) or isinstance(saved, dict)
        if vscode and not _ai_off(layers):
            if "chat request" not in evidence and "hook records" not in evidence:
                tr["caveats"].append(LOG_ONLY if evidence else NO_EVIDENCE)
            else:
                if isinstance(saved, dict) and _completions_on(layers):
                    out["completion_windows"].append({"tool": "copilot", "start": None, "end": None,
                                                      "what": SUGGESTIONS})
                # inline chat (Cmd/Ctrl-I) is never stored (chatServiceImpl.ts shouldStoreSession): it is
                # taken to be at hand while Copilot was being used, not for the whole session
                lo, hi = (min(used) - ACTIVITY_MARGIN, max(used) + ACTIVITY_MARGIN) if used else (None, None)
                out["completion_windows"].append({"tool": "copilot", "start": lo, "end": hi, "what": INLINE_CHAT})
        if problems:
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            out["coverage"]["format"] = UNRECOGNISED + problems[0] + more
        out["coverage"]["parsed"] = ok > 0 or tried == 0
        out["coverage"]["version"] = "; ".join(sorted(ctx["versions"])) or None
        return out

    def from_hook(self, rec: dict) -> dict | None:
        name, inp = rec.get("tool_name") or "?", rec.get("tool_input")
        fl = _fields(name, inp)
        return {"name": name, "input": inp, "op": fl["op"], "writes": fl.get("writes"), "command": fl.get("command")}

    def cross_checkable(self, rec: dict) -> bool:
        return rec.get("tool_name") not in NEVER_STORED


ADAPTER = Copilot()
