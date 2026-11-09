"""Cursor: the editor's agent chats, Cmd-K, the cursor-agent CLI, and the hooks they run.

Where Cursor keeps a conversation. Read from the code of Cursor 3.22.12 and
cursor-agent 2026.09.28; the editor on the machine this was written on was
2.5.26 and nearly unused, so none of it is checked against a real 3.22
session yet (tests/record/cursor.md lists what to check).

  <user data>/User/globalStorage/state.vscdb   every workspace's chats, one file
      cursorDiskKV  composerData:<id>          a conversation: the ordered bubble list
                    bubbleId:<id>:<bubble>     one message; each tool call is its own bubble
                    composer.content.<sha256>  a whole file before or after an agent edit
      the index     the composerHeaders table (3.22), the ItemTable key
                    composer.composerHeaders (3.0), each workspace's own
                    composer.composerData (2.6 and before)
  <user data>/User/workspaceStorage/<id>/      workspace.json names the folder
  <config>/chats/<md5 of cwd>/<id>/            a CLI chat: store.db, meta.json
  <data>/projects/<slug>/agent-transcripts/    JSONL both write: no times, no ids
  ~/.cursor/ai-tracking/ai-code-tracking.db    Cursor's own per-line AI attribution

<user data> is ~/Library/Application Support/Cursor or its Linux and Windows
equivalent (IVKIT_CURSOR_USER_DATA stands in for a --user-data-dir); <config>
is $CURSOR_CONFIG_DIR, $XDG_CONFIG_HOME/cursor or ~/.cursor; <data> is
$CURSOR_DATA_DIR or ~/.cursor.

Cmd-K (inline edit) keeps no conversation. Its afterFileEdit hook record is
the only record of the text it wrote, so parse turns each such record into
the agent's edit.

Editing an earlier prompt, or restoring a checkpoint, and sending again
deletes every later bubble. The hook log keeps what the store lost, and the
cross-check reports the gap.

What leaves the machine. Only what this session did in this folder: chats
active during the session (from .interview/recorder.json's start), reduced to
the messages from the session; of a chat in another folder, only the tool
calls that worked here; file text only for files here; never the editor's
settings (they hold the login) or a chat's encryption keys.
"""
from __future__ import annotations
import collections, datetime, difflib, hashlib, json, os, pathlib, re, shutil, sqlite3, tempfile, time
from urllib.parse import quote, unquote, urlparse

from . import base
from .. import paths as P

# aiserver.v1.ClientSideToolV2, the `tool` of a bubble's toolFormerData
EDIT_FILE, DELETE_FILE, RUN_TERMINAL_COMMAND_V2, TASK, EDIT_FILE_V2, TASK_V2 = 7, 11, 15, 32, 38, 48
# Model-level tool names too: the CLI's messages and the transcripts carry
# those, and older chats used the snake_case ones.
EDIT_NAMES = ("search_replace", "write", "StrReplace", "Write", "ApplyPatch", "EditNotebook", "MultiEdit",
              "delete_file", "Delete")
DELETE_NAMES = ("delete_file", "Delete")
COMMAND_NAMES = ("run_terminal_cmd", "Shell")
AGENT_NAMES = ("task", "task_v2", "Task")
# The tool name Cursor's preToolUse hook gives a model-level call: every edit
# goes through one Write
HOOK_NAME = {"StrReplace": "Write", "Write": "Write", "ApplyPatch": "Write", "EditNotebook": "Write",
             "MultiEdit": "Write", "search_replace": "Write", "write": "Write", "Delete": "Delete",
             "delete_file": "Delete", "Shell": "Shell", "run_terminal_cmd": "Shell"}
COMPOSER_V = range(3, 19)       # 3.22.12 migrates composerData up to _v 18; before 3 bubbles were inline
BUBBLE_V = (None, 1, 2, 3)
# .cursor/hooks.json event -> hook_log.py event
HOOKS = (("sessionStart", "session_start"), ("beforeSubmitPrompt", "user_prompt"), ("preToolUse", "tool_use"),
         ("postToolUse", "tool_result"), ("afterFileEdit", "file_edit"), ("beforeShellExecution", "tool_use"),
         ("afterShellExecution", "tool_result"), ("afterTabFileEdit", "tab_edit"), ("stop", "stop"))
# Context Cursor sends wearing the user's role; cursor-agent's own transcript
# writer strips exactly these blocks.
INJECTED = ("user_info", "project_layout", "rules", "always_applied_workspace_rules",
            "agent_requestable_workspace_rules", "user_rules", "agent_skills", "available_skills",
            "cloud_instructions", "cloud_task_instructions", "open_and_recently_viewed_files", "system_reminder",
            "system-reminder", "instructions_update", "mcp_instructions", "mcp_file_system",
            "mcp_file_system_servers", "git_status", "agent_transcripts", "cursor_rules_context", "attached_files",
            "system_notification", "task_notification", "agent_notification")
INJECTED_RE = re.compile(r"<(%s)(?:\s[^>]*)?>[\s\S]*?</\1>" % "|".join(map(re.escape, INJECTED)), re.I)
CLIP = "...[+"                  # hook_log.clip's mark
WINDOW_SLACK = 600.0            # seconds before the recorder started that still count as the session
STORE_CAP = 2 * 1024 ** 3       # a store larger than this is not copied
HEADROOM = 256 * 1024 ** 2      # free disk a copy must leave
CONTENT_CAP = 150 * 1024 ** 2   # file text kept per editor store; collect's whole budget is 400 MB
DIFF_CELLS = 1_000_000          # past this, the line diff counts lines instead of aligning them
TAB_SLACK = 10.0                # seconds between a Tab accept's hook record and the tracker's rows for it
EDIT_BEFORE, EDIT_AFTER = 600.0, 30.0   # a file_edit record this soon after a known edit of its file is that edit
# What the parser reads of a conversation, a header and, for a chat in
# another folder, a message. The rest holds draft text, other files' text and
# the chat's encryption keys.
COMPOSER_KEEP = ("_v", "composerId", "name", "createdAt", "lastUpdatedAt", "fullConversationHeadersOnly",
                 "conversationMap", "status", "unifiedMode", "forceMode", "modelConfig", "workspaceIdentifier",
                 "subagentInfo", "subagentComposerIds", "subComposerIds", "isAgentic", "isNAL",
                 "createdFromBackgroundAgent", "gitWorktree", "isArchived", "isBestOfNSubcomposer")
HEADER_KEEP = ("type", "composerId", "name", "createdAt", "lastUpdatedAt", "workspaceIdentifier", "subagentInfo",
               "isArchived", "unifiedMode", "conversationCheckpointLastUpdatedAt")
BUBBLE_KEEP = ("_v", "type", "bubbleId", "createdAt", "requestId", "timingInfo", "capabilityType", "toolFormerData",
               "isAgentic", "unifiedMode", "serverBubbleId")
ELSEWHERE = ("name", "workspaceIdentifier")     # another folder's chat: its title and folder stay behind


# ---------------------------------------------------------------- where things are

def user_data_roots() -> list[pathlib.Path]:
    """The editor's user data directories, each the parent of User/."""
    env = os.environ.get("IVKIT_CURSOR_USER_DATA")
    if env:
        return [pathlib.Path(env)]
    out: list[pathlib.Path] = []
    for b in P.app_support():
        if b / "Cursor" not in out:
            out.append(b / "Cursor")
    return out


def config_dir() -> pathlib.Path:
    """cursor-agent's own rule, for chats/."""
    c, x = os.environ.get("CURSOR_CONFIG_DIR", ""), os.environ.get("XDG_CONFIG_HOME", "")
    if c.strip():
        return pathlib.Path(c)
    return pathlib.Path(x) / "cursor" if x.strip() else pathlib.Path.home() / ".cursor"


def data_dir() -> pathlib.Path:
    """The editor's and cursor-agent's rule, for projects/."""
    d = os.environ.get("CURSOR_DATA_DIR", "")
    return pathlib.Path(d) if d.strip() else pathlib.Path.home() / ".cursor"


def tracking_db() -> pathlib.Path:
    """Both write here whatever the environment says."""
    return pathlib.Path.home() / ".cursor" / "ai-tracking" / "ai-code-tracking.db"


def app_roots() -> list[pathlib.Path]:
    """Where the installed app's package.json may be."""
    home = pathlib.Path.home()
    if P.IS_MAC:
        return [b / "Cursor.app" / "Contents" / "Resources" / "app" / "package.json"
                for b in (pathlib.Path("/Applications"), home / "Applications")]
    if P.IS_WIN:
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return [pathlib.Path(local) / "Programs" / "cursor" / "resources" / "app" / "package.json"]
    return [pathlib.Path(d) / "resources" / "app" / "package.json"
            for d in ("/usr/share/cursor", "/opt/Cursor", "/opt/cursor", str(home / ".local" / "share" / "cursor"))]


def cli_roots() -> list[pathlib.Path]:
    """cursor-agent's install script keeps one directory per version here."""
    home = pathlib.Path.home()
    if P.IS_WIN:
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return [pathlib.Path(local) / "cursor-agent" / "versions"]
    return [home / ".local" / "share" / "cursor-agent" / "versions"]


def slug(path: str) -> str:
    """The projects/ directory name of a workspace path. Not one to one:
    take-home, take_home and take.home share one."""
    return re.sub(r"^-+|-+$", "", re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]", "-", path)))


def _enc(cid: str) -> str:
    """A conversation id as a transcript file name: encodeURIComponent, % as _."""
    return quote(str(cid), safe="-_.!~*'()").replace("%", "_")[:200]


def _free(path) -> int:
    return shutil.disk_usage(str(path)).free


# ---------------------------------------------------------------- values as the stores keep them

def _s(v) -> str | None:
    """An id, a path or a name: a non-empty string, or nothing. A store in a
    shape nobody foresaw must not hand trace a dict where it keys on a string."""
    return v if isinstance(v, str) and v else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch(v) -> float | None:
    """ISO 8601 (any fraction, Z or an offset; none is local time) or epoch
    seconds. trace.epoch's rule, here because collect runs where trace is not."""
    if _num(v) is not None:
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


def _under(p, root: pathlib.Path) -> bool:
    if not isinstance(p, str) or not os.path.isabs(p):
        return False
    try:
        r = pathlib.Path(p).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return r == root or root in r.parents


def _same_path(a, b) -> bool:
    """Two spellings of one file: equal, or one relative and the other's tail."""
    if not (isinstance(a, str) and isinstance(b, str) and a and b):
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _uri_path(u) -> str | None:
    if isinstance(u, dict):
        return _s(u.get("fsPath")) or _s(u.get("path"))
    if isinstance(u, str) and u.startswith("file://"):
        p = unquote(urlparse(u).path)
        return _s(p[1:] if re.match(r"^/[A-Za-z]:", p) else p)
    return None


def _fs(h) -> str | None:
    """The folder a header or composerData places its chat in."""
    wi = h.get("workspaceIdentifier") if isinstance(h, dict) else None
    return _uri_path(wi.get("uri")) if isinstance(wi, dict) else None


def _folder(wd: pathlib.Path) -> str | None:
    """The folder a workspaceStorage entry had open."""
    try:
        return _uri_path(json.loads((wd / "workspace.json").read_text()).get("folder"))
    except (OSError, ValueError, AttributeError):
        return None


def _json_file(p: pathlib.Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _loads(v):
    """A cursorDiskKV or ItemTable value: JSON text when it is JSON."""
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _obj(v) -> dict:
    """A proto message the editor stored as its JSON text, or kept as an object."""
    v = _loads(v)
    return v if isinstance(v, dict) else {}


def _text(v) -> str | None:
    """A composer.content row: the file's raw text."""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return v if isinstance(v, str) else None


def _first(*vs) -> str | None:
    return next((v for v in vs if isinstance(v, str) and v), None)


def _cmd(v) -> str:
    """A command line, whether the store kept a string or an argv."""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return v if isinstance(v, str) else ""


def _keep(d: dict, keys) -> dict:
    return {k: d[k] for k in keys if k in d}


def _added(before: str, after: str) -> str:
    """The lines an edit put in: the + side of before -> after. The common
    head and tail are cut first; a middle too big to align (a large file's
    repetitive lines make the aligner quadratic) gives the lines `after` has
    more of than `before`, which leaves out only lines the edit moved."""
    a, b = before.splitlines(), after.splitlines()
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    j = 0
    while j < len(a) - i and j < len(b) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    a, b = a[i:len(a) - j], b[i:len(b) - j]
    if not a or not b:
        return "\n".join(b)
    if len(a) * len(b) <= DIFF_CELLS:
        ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
        return "\n".join(ln for tag, _, _, j1, j2 in ops if tag in ("replace", "insert") for ln in b[j1:j2])
    left = collections.Counter(a)
    out = []
    for ln in b:
        if left[ln] > 0:
            left[ln] -= 1
        else:
            out.append(ln)
    return "\n".join(out)


def _patch(text: str) -> dict[str, str]:
    """{path: + lines} of an apply_patch text."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in str(text).splitlines():
        m = re.match(r"\*\*\* (?:Add|Update) File: (.+)$", line)
        if m:
            cur = out.setdefault(m.group(1).strip(), [])
        elif line.startswith("*** "):
            cur = cur if line.startswith("*** Move to:") else None
        elif cur is not None and line.startswith("+"):
            cur.append(line[1:])
    return {p: "\n".join(v) for p, v in out.items()}


def _edits(v) -> list[dict]:
    """The edits of an afterFileEdit or afterTabFileEdit payload. hook_log keeps
    them as the payload's JSON text, clipped: a cut list still yields the
    new_string values that are whole."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            out = []
            for m in re.findall(r'"new_string"\s*:\s*"((?:[^"\\]|\\.)*)"', v):
                try:
                    out.append({"new_string": json.loads('"' + m + '"')})
                except ValueError:
                    continue
            return out
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def _new_text(edits) -> str:
    return "\n".join(e["new_string"] for e in _edits(edits) if isinstance(e.get("new_string"), str) and e["new_string"])


def _op(tool, name: str) -> str:
    if tool in (TASK, TASK_V2) or name in AGENT_NAMES:
        return base.AGENT
    if tool in (EDIT_FILE, DELETE_FILE, EDIT_FILE_V2) or name.startswith("edit_file") or name in EDIT_NAMES:
        return base.EDIT
    if tool == RUN_TERMINAL_COMMAND_V2 or name.startswith("run_terminal_command") or name in COMMAND_NAMES:
        return base.COMMAND
    return base.OTHER


def _model_call(name: str, args) -> tuple:
    """(op, writes, command, cwd) of a call as the model made it: the CLI's
    messages, the transcripts and the hook payloads all use these names."""
    if isinstance(args, str):
        args = _obj(args) or ({"patch": args} if "*** Begin Patch" in args else {})
    args = args if isinstance(args, dict) else {}
    op = _op(None, name)
    writes = command = cwd = None
    if op == base.EDIT:
        path = _first(args.get("path"), args.get("file_path"), args.get("target_file"),
                      args.get("target_notebook")) or ""
        if name in DELETE_NAMES:
            writes = {}
        elif "*** Begin Patch" in str(args.get("patch") or args.get("input") or ""):
            writes = _patch(args.get("patch") or args.get("input"))
        else:
            text = _first(args.get("contents"), args.get("content"), args.get("fileText"), args.get("new_string"),
                          args.get("code_edit"))
            writes = {path: text} if text is not None else None
    elif op == base.COMMAND:
        command = _cmd(args.get("command"))
        cwd = _first(args.get("working_directory"), args.get("workingDirectory"), args.get("cwd"))
    return op, writes, command, cwd


def _tool_paths(b: dict) -> list:
    """Every path a bubble's tool call names."""
    tf = _obj(b.get("toolFormerData"))
    return [v for d in (_obj(tf.get("params")), _obj(tf.get("rawArgs")))
            for k in ("relativeWorkspacePath", "path", "file_path", "targetFile", "target_file", "cwd",
                      "workingDirectory", "working_directory") for v in [d.get(k)] if isinstance(v, str)]


def _bubble_ms(b) -> float | None:
    """A bubble's own time, machine epoch ms."""
    if not isinstance(b, dict):
        return None
    t = _epoch(b.get("createdAt")) if isinstance(b.get("createdAt"), str) else None
    if t is not None:
        return t * 1000
    return _num(_obj(b.get("timingInfo")).get("clientStartTime"))


# ---------------------------------------------------------------- the editor's tool bubbles

def _applied(tf: dict, op: str, result: dict, add: dict) -> bool:
    """False only when the call cannot have changed anything: rejected, never
    started, or failed before it ran. A command that ran and exited non-zero,
    timed out or was stopped mid-run changed what it changed, so provenance
    must still see it as the agent's. A write held for review keeps its
    before and after ids when the candidate rejects it."""
    status, astatus = str(tf.get("status") or ""), str(add.get("status") or "")
    if tf.get("userDecision") == "rejected" or result.get("rejected") is True or astatus == "rejected":
        return False
    if op == base.COMMAND and (result.get("exitCode") is not None or add.get("startedAtMs") is not None
                               or "timed out" in str(tf.get("error") or "").lower()):
        return True
    if op == base.EDIT and result.get("afterContentId"):
        return True                                 # the file's text after the edit was stored: it landed
    return status not in ("cancelled", "error") and astatus not in ("cancelled", "error")


def _error(tf: dict, op: str, result: dict, add: dict) -> str | None:
    """What went wrong, as the timeline shows it: `exit <n>` for a command that ran."""
    status, astatus = str(tf.get("status") or ""), str(add.get("status") or "")
    code = result.get("exitCode")
    if tf.get("userDecision") == "rejected" or result.get("rejected") is True or astatus == "rejected":
        return "rejected"
    if op == base.COMMAND and isinstance(code, int) and code:
        return f"exit {code}"
    if "error" in (status, astatus):
        return str(_obj(tf.get("error")).get("clientVisibleErrorMessage") or "error")[:200]
    return "cancelled" if "cancelled" in (status, astatus) else None


def _index(table, key, workspaces: list[dict]) -> tuple[dict, str | None]:
    """({composerId: header}, which index came first). The 3.22 table wins
    over the 3.0 ItemTable key, which wins over a workspace's allComposers;
    `table` and `key` are None when the store has none."""
    headers: dict = {}
    first = None
    lists = [("table", [_loads(v) for v in table] if table is not None else None),
             ("key", (_obj(key).get("allComposers") or []) if key is not None else None),
             ("workspace", [h for w in workspaces for h in w.get("allComposers") or []] if workspaces else None)]
    for name, rows in lists:
        if rows is None:
            continue
        first = first or name
        for h in rows if isinstance(rows, list) else []:
            if isinstance(h, dict) and _s(h.get("composerId")):
                headers.setdefault(h["composerId"], h)
    return headers, first


def _is_sub(c: str, *infos) -> str | None:
    """The parent of a subagent conversation, '' when it is one whose parent is
    not named, None when it is not one."""
    for si in infos:
        p = _s(_obj(si).get("parentComposerId"))
        if p:
            return p
    return "" if c.startswith("task-") else None


# ---------------------------------------------------------------- cursor-agent's store.db

def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def _proto(b: bytes) -> list[tuple[int, int, object]]:
    """[(field, wire type, int or bytes)]: the wire format, no schema."""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt in (1, 5):
            v, i = b[i:i + (8 if wt == 1 else 4)], i + (8 if wt == 1 else 4)
        elif wt == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError(f"wire type {wt}")
        if i > len(b):
            raise ValueError("truncated field")
        out.append((f, wt, v))
    return out


def _all(fields, no: int) -> list:
    return [v for f, _, v in fields if f == no]


def _one(fields, no: int):
    vs = _all(fields, no)
    return vs[0] if vs else None


def _str(fields, no: int) -> str:
    v = _one(fields, no)
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def _int(fields, no: int) -> int | None:
    v = _one(fields, no)
    return v if isinstance(v, int) else None


# agent.v1.ToolCall member -> {result case field: what it means}. The
# members' own field 2 is the result: ShellResult, DeleteResult, EditResult,
# McpResult. A case not listed ran.
OUTCOMES = {1: {4: "rejected", 5: "failed", 7: "failed"},                    # shell
            3: {6: "rejected", 2: "failed", 3: "failed", 4: "failed", 5: "failed", 7: "failed"},   # delete
            12: {6: "rejected", 2: "failed", 3: "failed", 4: "failed", 7: "failed"},              # edit
            15: {3: "rejected", 2: "failed", 4: "failed", 5: "failed", 6: "failed"}}              # mcp


def _outcome(call) -> tuple[bool, str | None]:
    """(applied, error) of one agent.v1.ToolCall."""
    for member, cases in OUTCOMES.items():
        m = _one(call, member)
        res = _one(_proto(m), 2) if isinstance(m, bytes) else None
        if not isinstance(res, bytes):
            continue
        for f, _, v in _proto(res):
            if f in cases:
                return False, cases[f]
            if member == 1 and f == 2 and isinstance(v, bytes):          # ShellFailure: it ran
                code = _int(_proto(v), 3)
                return True, f"exit {code}" if code else "failed"
        return True, None
    return True, None


def _user_text(content) -> str:
    """What the candidate typed: <user_query> when there is one, else the text
    without the context Cursor injects."""
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
    raw = "\n".join(p["text"] for p in parts if isinstance(p, dict) and p.get("type") == "text"
                    and isinstance(p.get("text"), str)) if isinstance(parts, list) else ""
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", raw, re.S)
    if m:
        return m.group(1).strip()
    return re.sub(r"\n{3,}", "\n\n", INJECTED_RE.sub("", raw)).strip()


def _norm(s: str) -> str:
    return " ".join(str(s).split())


def _ro(db: pathlib.Path) -> sqlite3.Connection:
    """A collected copy: never written, no journal beside it."""
    return sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)


def _tables(con: sqlite3.Connection) -> set:
    return {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}


class _Reader:
    """One parse of agent_logs/cursor/: what was read, what was not understood."""

    def __init__(self, name: str, tr: dict, logs: pathlib.Path, bad):
        from .. import trace
        self.name, self.tr, self.logs, self.bad, self.trace = name, tr, logs, bad, trace
        self.out = base.parsed()
        self.hooks = [h for h in tr.get("hooks") or [] if isinstance(h, dict) and h.get("tool") == name]
        self.hook_t: dict = {}                  # tool_use_id -> the earliest hook record's time
        for h in self.hooks:
            u, t = _s(h.get("tool_use_id")), _num(h.get("t"))
            if h.get("event") == "tool_use" and u and t is not None:
                self.hook_t[u] = min(t, self.hook_t.get(u, t))
        self.issues: list[str] = []
        self.read: set = set()
        self.seen: set = set()                  # conversations some store holds, as transcript names
        self.native: set = set()                # the same, as session ids
        self.counts: collections.Counter = collections.Counter()
        self.editor_chats = self.windows = 0
        self.tracked: list[tuple] = []
        self.hook_only: set = set()

    def rel(self, p: pathlib.Path) -> str:
        return f"{self.name}/{p.relative_to(self.logs).as_posix()}"

    def issue(self, what: str) -> None:
        if what not in self.issues:
            self.issues.append(what)

    def when(self, local: float | None) -> float | None:
        return self.trace.server_time(self.tr, local) if local is not None else None

    @staticmethod
    def span(info: dict, t: float | None) -> None:
        if t is not None:
            info["first"] = t if info["first"] is None else min(info["first"], t)
            info["last"] = t if info["last"] is None else max(info["last"], t)

    def date(self, seq: list, info: dict) -> None:
        """A call with no time of its own takes its hook record's, else the
        nearest dated record's before it (after it, for the first): provenance
        drops a call it cannot place in time, which would credit its lines to
        the candidate."""
        times = [r.get("t") for r in seq]
        for k, r in enumerate(seq):
            if r.get("kind") != "tool_call" or r.get("t") is not None:
                continue
            t, how = (self.hook_t.get(r["id"]) if r.get("id") else None), "hook"
            if t is None:
                how = "neighbour"
                t = next((times[j] for j in range(k - 1, -1, -1) if times[j] is not None), None)
                if t is None:
                    t = next((times[j] for j in range(k + 1, len(seq)) if times[j] is not None), None)
            else:
                times[k] = t
            if t is not None:
                r["t"], r["t_from"] = t, how
                self.span(info, t)

    # ------------------------------------------------ the editor

    def editor(self, d: pathlib.Path) -> None:
        """editor/: globalStorage/state.vscdb and workspaceStorage/<id>/, as collect reduced them."""
        g = d / "globalStorage" / "state.vscdb"
        kv, key, table = {}, None, None
        if g.exists():
            self.read.add(g)
            con = _ro(g)
            try:
                t = _tables(con)
                if "cursorDiskKV" in t:
                    kv = dict(con.execute("select key, value from cursorDiskKV"))
                if "ItemTable" in t:
                    r = con.execute("select value from ItemTable where key = 'composer.composerHeaders'").fetchone()
                    key = r[0] if r else None
                if "composerHeaders" in t:
                    table = [v for (v,) in con.execute("select value from composerHeaders")]
            finally:
                con.close()
        workspaces = []
        for w in sorted(d.glob("workspaceStorage/*/state.vscdb")):
            self.read.update((w, w.parent / "workspace.json"))
            self.windows += 1
            con = _ro(w)
            try:
                v = con.execute("select value from ItemTable where key = 'composer.composerData'").fetchone()
            finally:
                con.close()
            if v is not None:
                workspaces.append({"folder": _folder(w.parent), "allComposers": _obj(v[0]).get("allComposers") or []})
        self.composers(kv, key, table, workspaces, self.rel(g))

    def legacy(self, f: pathlib.Path) -> None:
        """What collect.vscode_family wrote before this adapter: one JSON bundle
        per workspace, the bodies joined in, no file text."""
        self.read.add(f)
        b = _json_file(f)
        if not isinstance(b, dict):
            self.issue(f"{self.rel(f)} is not JSON")
            return
        idx = b.get("composer.composerData")
        ws = [{"folder": None, "allComposers": _obj(idx).get("allComposers") or []}] if idx is not None else []
        bodies = b.get("composerBodies")
        self.composers(bodies if isinstance(bodies, dict) else {}, None, None, ws, self.rel(f))

    def composers(self, kv: dict, key, table, workspaces: list, rel: str) -> None:
        headers, index = _index(table, key, workspaces)
        folders = {h.get("composerId"): w.get("folder") for w in workspaces for h in w.get("allComposers") or []
                   if isinstance(h, dict) and _s(h.get("composerId"))}
        ids = set(headers) | {k[len("composerData:"):] for k in kv if isinstance(k, str) and k.startswith("composerData:")}
        if ids and index is None:
            self.issue(f"no composer index in {rel}")
        datas = {c: _loads(kv.get("composerData:" + c)) for c in ids}
        body = lambda c: datas[c] if isinstance(datas[c], dict) else {}
        sub_of = lambda c: _is_sub(c, body(c).get("subagentInfo"), (headers.get(c) or {}).get("subagentInfo"))
        created = lambda c: _num(body(c).get("createdAt")) or _num((headers.get(c) or {}).get("createdAt")) or 0
        spawns: dict = {}
        for c in sorted(ids, key=lambda c: (sub_of(c) is not None, created(c), c)):
            d = datas[c]
            if not isinstance(d, dict):
                self.counts["no_body"] += 1           # listed, but its conversation is not here (deleted, or cloud)
                continue
            if d.get("_v") not in COMPOSER_V:
                self.issue(f"composerData _v {d.get('_v')!r}")
            parent = sub_of(c)
            si = _obj(d.get("subagentInfo")) or _obj((headers.get(c) or {}).get("subagentInfo"))
            typ = _s(si.get("subagentTypeName")) or "task" if parent is not None else None
            actor = f"subagent:{typ}" if parent is not None else self.name
            cwd = _fs(headers.get(c)) or _fs(d) or _s(folders.get(c))
            info = base.session_info(self.name, c, f"{rel}:composerData:{c}", actor, cwd)
            model = _s(_obj(d.get("modelConfig")).get("modelName"))
            if model:
                info["models"].add(model)
            self.seen.add(_enc(c))
            self.native.add(c)
            self.editor_chats += 1
            inline = d.get("conversationMap") if isinstance(d.get("conversationMap"), dict) else {}
            seq: list = []
            heads = d.get("fullConversationHeadersOnly")
            for h in heads if isinstance(heads, list) else []:
                bid = _s(h.get("bubbleId")) if isinstance(h, dict) else None
                if not bid:
                    continue
                b = _loads(kv.get(f"bubbleId:{c}:{bid}"))
                b = b if isinstance(b, dict) else inline.get(bid)
                if not isinstance(b, dict):
                    self.counts["lost_bubbles"] += 1
                    continue
                if b.get("_v") not in BUBBLE_V:
                    self.issue(f"bubble _v {b.get('_v')!r}")
                try:
                    r = self.bubble(kv, c, b, info, actor, parent is not None, f"{rel}:bubbleId:{c}:{bid}", spawns)
                except (AttributeError, TypeError, ValueError, KeyError):
                    self.bad[rel] += 1                # one bubble in a shape nobody foresaw; the rest stands
                    continue
                if r is not None:
                    seq.append(r)
            self.date(seq, info)
            info["models"] = sorted(info["models"])
            if parent is None:
                self.out["sessions"].append(info)
                continue
            spawn = spawns.get(c) or spawns.get(_s(si.get("toolCallId"))) or {}
            info.update(type=typ, parent=parent or None, spawn_ref=spawn.get("ref"),
                        description=_s(_obj(spawn.get("input")).get("description")))
            self.out["subagents"].append(info)

    def bubble(self, kv: dict, cid: str, b: dict, info: dict, actor: str, sub: bool, ref: str,
               spawns: dict) -> dict | None:
        """The record one bubble makes, if any."""
        ms_ = _bubble_ms(b)
        local = ms_ / 1000 if ms_ is not None else None
        t = self.when(local)
        self.span(info, t)
        text = b.get("text") if isinstance(b.get("text"), str) else ""
        if b.get("type") == 1:
            if not text.strip():
                return None
            if sub:                                   # a subagent's first message is its brief
                if info["brief"] is None:
                    info["brief"] = {"t": t, "text": text, "ref": ref}
                    e = base.event(t, self.name, "brief", actor, text, ref, session=cid)
                    self.out["events"].append(e)
                    return e
                return None
            info["prompts"] += 1
            p = base.prompt(t, local, self.name, text, cid, ref)
            self.out["prompts"].append(p)
            return p
        if b.get("type") != 2:
            return None
        tf = b.get("toolFormerData")
        if isinstance(tf, dict) and (tf.get("tool") or tf.get("name")):
            c = self.tool(kv, tf, t, ref, cid, actor, info)
            for k in (_s(_obj(tf.get("additionalData")).get("subagentComposerId")), _s(tf.get("toolCallId"))):
                if c["op"] == base.AGENT and k:
                    spawns[k] = c
            return c
        if text.strip():
            e = base.agent_text(t, self.name, actor, text, ref, cid)
            self.out["events"].append(e)
            return e
        return None

    def tool(self, kv: dict, tf: dict, t, ref: str, cid: str, actor: str, info: dict) -> dict:
        params, result, raw = _obj(tf.get("params")), _obj(tf.get("result")), _obj(tf.get("rawArgs"))
        add = _obj(tf.get("additionalData"))
        tool = tf.get("tool") if _num(tf.get("tool")) is not None else None
        name = _s(tf.get("name")) or (f"tool {int(tool)}" if tool is not None else "?")
        op = _op(tool, name)
        inp = params or raw
        path = _first(params.get("relativeWorkspacePath"), raw.get("path"), raw.get("file_path"),
                      raw.get("target_file"), params.get("targetFile")) or ""
        writes = command = cwd = None
        applied = _applied(tf, op, result, add)
        if op == base.EDIT:
            writes, missing = self.writes(kv, tool, name, path, params, result, raw, add)
            if missing or (writes is None and applied):
                self.counts["missing_content"] += 1
        elif op == base.COMMAND:
            command = _cmd(params.get("command")) or _cmd(raw.get("command"))
            cwd = _first(params.get("cwd"), raw.get("workingDirectory"), raw.get("working_directory")) or info["cwd"]
        what = path if op == base.EDIT and path else command if op == base.COMMAND else self.trace.summary(name, inp)
        c = base.tool_call(t, self.name, actor, str(what), ref, name=name, input=inp, op=op, session=cid,
                           id=_s(tf.get("toolCallId")), writes=writes, command=command,
                           applied=applied, error=_error(tf, op, result, add), cwd=cwd)
        info["calls"] += 1
        self.out["events"].append(c)
        return c

    def writes(self, kv: dict, tool, name: str, path: str, params: dict, result: dict, raw: dict,
               add: dict) -> tuple[dict | None, bool]:
        """({path: the lines the edit put in}, whether text the store names is
        missing). 3.x keeps the whole file before and after each edit, or,
        when the agent sent no full states, only a line diff; older chats kept
        the new text in the call itself."""
        if tool == DELETE_FILE or name in DELETE_NAMES:
            return {}, False
        after_id, before_id = _s(result.get("afterContentId")), _s(result.get("beforeContentId"))
        if after_id:
            after = _text(kv.get(after_id))
            if after is None:
                return None, True
            before = _text(kv.get(before_id)) if before_id else ""       # no before: a new file
            return {path: _added(before if before is not None else "", after)}, before is None
        lines = _obj(add.get("precomputedDiff")).get("lines")
        if isinstance(lines, list) and lines:
            got = [str(x.get("content") or "") for x in lines if isinstance(x, dict) and x.get("type") == "added"]
            return {path: "\n".join(got)}, False
        text = _first(params.get("contents"), params.get("new_string"), params.get("streamingContent"),
                      raw.get("contents"), raw.get("new_string"), raw.get("streamContent"), raw.get("code_edit"))
        return ({path: text} if text else None), False

    # ------------------------------------------------ cursor-agent

    def cli(self, db: pathlib.Path) -> None:
        """One chat's store.db. meta row '0' is the hex of a JSON object whose
        latestRootBlobId names the conversation's current root; blobs are
        SHA-256 addressed. The root is protobuf (agent.v1.ConversationStateStructure):
        field 1 lists the message blobs, JSON with a role, fields 13 and 11 the
        archives of summarised messages, field 8 the turns, which carry the
        times and each call's outcome. Blobs no root reaches are abandoned
        branches and are not read."""
        rel = self.rel(db)
        self.read.update((db, db.parent / "meta.json"))
        mj = _json_file(db.parent / "meta.json")
        mj = mj if isinstance(mj, dict) else {}
        con = _ro(db)
        try:
            blob = lambda i: (lambda r: r[0] if r else None)(
                con.execute("select data from blobs where id = ?", (i.hex() if isinstance(i, bytes) else i,)).fetchone())
            uv = con.execute("pragma user_version").fetchone()[0]
            row = con.execute("select value from meta where key = '0'").fetchone()
            try:
                meta = json.loads(bytes.fromhex(row[0]).decode("utf-8"))
                root = blob(str(meta["latestRootBlobId"]))
            except (TypeError, ValueError, KeyError, AttributeError):
                self.issue(f"{rel}: its meta row is not readable")
                return
            if uv not in (0, 1):
                self.issue(f"{rel}: store.db user_version {uv}")
            if root is None:
                if con.execute("select count(*) from blobs").fetchone()[0]:
                    self.issue(f"{rel}: no root blob")
                return
            try:
                self.conversation(rel, bytes(root), blob, meta, mj)
            except ValueError as e:
                self.issue(f"{rel}: not understood ({e})")
        finally:
            con.close()

    def conversation(self, rel: str, root: bytes, blob, meta: dict, mj: dict) -> None:
        fields = _proto(root)
        ids = [i for a in _all(fields, 13) + _all(fields, 11) if isinstance(a, bytes)
               for i in _all(_proto(bytes(blob(a) or b"")), 1)]
        archived = set(ids)                     # the window tail after a summary may repeat archived messages
        ids += [i for i in _all(fields, 1) if i not in archived]
        users, says, calls, shells = self.turns(_all(fields, 8), blob)
        sid = _s(meta.get("agentId")) or pathlib.Path(rel).parent.name
        cwd = _s(mj.get("cwd"))
        info = base.session_info(self.name, sid, rel, self.name, cwd)
        if _s(meta.get("lastUsedModel")):
            info["models"].add(meta["lastUsedModel"])
        self.seen.add(_enc(sid))
        self.native.add(sid)

        def match(pool: list, text: str) -> float | None:
            """The machine time of the first unused turn record with this text."""
            n = _norm(text)
            for k, (s, v) in enumerate(pool):
                if s is not None and (s == n or (s and s in n)):
                    pool[k] = (None, v)
                    return v / 1000 if v else None
            return None
        seq: list = []
        for i in ids:
            if not isinstance(i, bytes):
                continue
            data = blob(i)
            ref = f"{rel}:blob:{i.hex()[:16]}"
            try:
                m = json.loads(bytes(data).decode("utf-8")) if data is not None else None
            except ValueError:
                m = None
            if not isinstance(m, dict):
                self.counts["unreadable_blobs"] += 1
                continue
            role, content = m.get("role"), m.get("content")
            if role == "system" or _obj(_obj(m.get("providerOptions")).get("cursor")).get("isSummary"):
                continue
            if role == "user":
                text = _user_text(content)
                if text:
                    local = match(users, text)
                    t = self.when(local)
                    info["prompts"] += 1
                    self.span(info, t)
                    p = base.prompt(t, local, self.name, text, sid, ref)
                    self.out["prompts"].append(p)
                    seq.append(p)
                continue
            if role != "assistant":
                continue
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else \
                list(content) if isinstance(content, list) else []
            parts += [{"type": "tool-call", "toolCallId": c.get("id"), "toolName": _obj(c.get("function")).get("name"),
                       "args": _obj(c.get("function")).get("arguments")}
                      for c in m.get("tool_calls") or [] if isinstance(c, dict)]
            for p in parts:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and isinstance(p.get("text"), str) and p["text"].strip():
                    t = self.when(match(says, p["text"]))
                    self.span(info, t)
                    e = base.agent_text(t, self.name, self.name, p["text"], ref, sid)
                    self.out["events"].append(e)
                    seq.append(e)
                elif p.get("type") == "tool-call":
                    cid = _s(p.get("toolCallId"))
                    start, end, applied, error = calls.get(cid, (None, None, True, None))
                    t = self.when(start / 1000) if start else None
                    self.span(info, t)
                    seq.append(self.model_call(t, ref, _s(p.get("toolName")) or "?", p.get("args"), sid, self.name,
                                               info, id=cid, t_result=self.when(end / 1000) if end else None,
                                               applied=applied, error=error))
        self.date(seq, info)
        for n, (command, code) in enumerate(shells):
            info["calls"] += 1                        # the candidate's own shell turn: no time is kept
            self.out["events"].append(base.tool_call(
                None, self.name, "candidate", command, f"{rel}:shell:{n}", name="shell", input={"command": command},
                op=base.COMMAND, session=sid, command=command, error=f"exit {code}" if code else None, cwd=cwd))
        info["models"] = sorted(info["models"])
        self.out["sessions"].append(info)

    def turns(self, turns: list, blob) -> tuple[list, list, dict, list]:
        """What the turns keep: user messages and assistant text (text, ms),
        tool calls {id: (started, completed, applied, error)}, the shell turns."""
        users, says, calls, shells = [], [], {}, []
        for tid in turns:
            tb = blob(tid) if isinstance(tid, bytes) else None
            for f, wt, v in _proto(bytes(tb)) if tb is not None else []:
                if f == 1 and wt == 2:                # AgentConversationTurnStructure
                    at = _proto(v)
                    for um in _all(at, 1):
                        ub = blob(um) if isinstance(um, bytes) else None
                        if ub is not None:
                            u = _proto(bytes(ub))
                            users.append((_norm(_str(u, 1)), _int(u, 25)))
                    for st in _all(at, 2):
                        sb = blob(st) if isinstance(st, bytes) else None
                        for sf, swt, sv in _proto(bytes(sb)) if sb is not None else []:
                            if sf == 1 and swt == 2:  # AssistantMessage
                                a = _proto(sv)
                                says.append((_norm(_str(a, 1)), _int(a, 2)))
                            elif sf == 2 and swt == 2:  # agent.v1.ToolCall
                                c = _proto(sv)
                                if _str(c, 57):
                                    calls[_str(c, 57)] = (_int(c, 59), _int(c, 60)) + _outcome(c)
                elif f == 2 and wt == 2:              # ShellConversationTurnStructure
                    s = _proto(v)
                    cb, ob = blob(_one(s, 1) or b""), blob(_one(s, 2) or b"")
                    shells.append((_str(_proto(bytes(cb)), 1) if cb is not None else "",
                                   _int(_proto(bytes(ob)), 3) if ob is not None else None))
        return users, says, calls, shells

    def model_call(self, t, ref: str, name: str, args, sid: str, actor: str, info: dict, **kw) -> dict:
        op, writes, command, cwd = _model_call(name, args)
        inp = args if isinstance(args, dict) else _obj(args) or args
        what = next(iter(writes), "") if writes else command if op == base.COMMAND else ""
        info["calls"] += 1
        e = base.tool_call(t, self.name, actor, what or self.trace.summary(name, inp), ref, name=name, input=inp,
                           op=op, session=sid, writes=writes, command=command,
                           cwd=cwd or (info["cwd"] if op == base.COMMAND else None), **kw)
        self.out["events"].append(e)
        return e

    # ------------------------------------------------ the transcripts

    def transcript(self, f: pathlib.Path, sub: bool) -> None:
        """No times and no call ids: read only for a conversation no store
        holds, and dated, where they can be, by this conversation's own hook
        records (the prompt's text, the call's tool and file or command)."""
        rel = self.rel(f)
        cid = f.stem
        parent = f.parent.parent.name if sub else None
        actor = "subagent:?" if sub else self.name
        info = base.session_info(self.name, cid, rel, actor)
        self.native.add(cid)
        mine = sorted((h for h in self.hooks if h.get("session_id") in (cid, parent) and _num(h.get("t")) is not None),
                      key=lambda h: h["t"])
        asks = [h for h in mine if h.get("event") == "user_prompt" and isinstance(h.get("prompt"), str)]
        uses = [h for h in mine if h.get("event") == "tool_use"]
        used: set = set()
        after = [float("-inf")]

        def hook_of(e: dict):
            """The first unused tool_use record, after the last one matched, for this call."""
            want = HOOK_NAME.get(e["name"], e["name"])
            target = next(iter(e["writes"]), None) if e.get("writes") else None
            for k, h in enumerate(uses):
                if k in used or h["t"] < after[0] or h.get("tool_name") != want:
                    continue
                ti = h.get("tool_input") if isinstance(h.get("tool_input"), dict) else {}
                if e["op"] == base.EDIT and target and not _same_path(ti.get("file_path"), target):
                    continue
                if e["op"] == base.COMMAND and _norm(ti.get("command") or "") != _norm(e.get("command") or ""):
                    continue
                used.add(k)
                after[0] = h["t"]
                return h
            return None
        seq: list = []
        for n, r in self.trace.rows(f, self.bad, rel):
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            content, ref = msg.get("content"), f"{rel}:{n}"
            if r.get("role") == "user":
                text = _user_text(content)
                if not text:
                    continue
                if sub:
                    if info["brief"] is None:
                        info["brief"] = {"t": None, "text": text, "ref": ref}
                        self.out["events"].append(base.event(None, self.name, "brief", actor, text, ref, session=cid))
                    continue
                h = next((h for h in asks if _norm(h["prompt"]) == _norm(text)), None)
                if h is not None:
                    asks.remove(h)
                t = h["t"] if h is not None else None
                self.span(info, t)
                info["prompts"] += 1
                p = base.prompt(t, h.get("local") if h is not None else None, self.name, text, cid, ref)
                self.out["prompts"].append(p)
                seq.append(p)
            elif r.get("role") == "assistant":
                parts = [{"type": "text", "text": content}] if isinstance(content, str) else \
                    content if isinstance(content, list) else []
                for p in parts:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and isinstance(p.get("text"), str) and p["text"].strip():
                        e = base.agent_text(None, self.name, actor, p["text"], ref, cid)
                        self.out["events"].append(e)
                        seq.append(e)
                    elif p.get("type") == "tool_use":
                        e = self.model_call(None, ref, _s(p.get("name")) or "?", p.get("input"), cid, actor, info)
                        h = hook_of(e)
                        if h is not None:
                            e.update(t=h["t"], id=_s(h.get("tool_use_id")), t_from="hook")
                            self.span(info, h["t"])
                        seq.append(e)
        self.date(seq, info)
        info["models"] = sorted(info["models"])
        if sub:
            info.update(type="?", parent=parent)
        self.out["subagents" if sub else "sessions"].append(info)

    # ------------------------------------------------ Cmd-K, Tab, and what was not understood

    def file_edits(self) -> None:
        """An afterFileEdit record no known edit accounts for is an edit the
        store does not hold: Cmd-K's (its conversation has no tool calls and
        no store), or an agent edit the store lost. Known: an edit of the file
        in the same conversation just before, in the store or as a tool hook
        record (trace makes those calls itself)."""
        known = collections.defaultdict(list)           # path -> [(session, t)]
        for e in self.out["events"]:
            if e["kind"] == "tool_call" and e.get("op") == base.EDIT:
                for p in list(e.get("writes") or {}) + [e.get("text")]:
                    if _s(p):
                        known[p].append((e.get("session"), e.get("t")))
        tooled = set()
        for h in self.hooks:
            ti = h.get("tool_input") if isinstance(h.get("tool_input"), dict) else {}
            if h.get("event") == "tool_use":
                tooled.add(h.get("session_id"))
                if _s(ti.get("file_path")):
                    known[ti["file_path"]].append((h.get("session_id"), _num(h.get("t"))))
        for h in self.hooks:
            ti = h.get("tool_input") if isinstance(h.get("tool_input"), dict) else {}
            path, sid, t = _s(ti.get("file_path")), _s(h.get("session_id")), _num(h.get("t"))
            if h.get("event") != "file_edit" or not path:
                continue
            near = lambda s2, t2, sid=sid, t=t: (s2 == sid or not s2 or not sid) and \
                (t is None or t2 is None or t - EDIT_BEFORE <= t2 <= t + EDIT_AFTER)
            if any(_same_path(p, path) and near(s2, t2) for p, xs in known.items() for s2, t2 in xs):
                continue
            cmdk = sid not in self.native and sid not in tooled
            name = "cmd-k" if cmdk else "file_edit"
            raw = ti.get("edits")
            self.out["events"].append(base.tool_call(
                t, self.name, self.name, path, _s(h.get("ref")) or "transcript.jsonl", name=name,
                input={"file_path": path}, op=base.EDIT, session=sid, writes={path: _new_text(raw)},
                clipped=CLIP in str(raw) or bool(h.get("clip"))))
            if cmdk and sid:
                self.hook_only.add(sid)

    def tracking(self, db: pathlib.Path) -> None:
        self.read.add(db)
        con = _ro(db)
        try:
            if "ai_code_hashes" in _tables(con):
                self.tracked = list(con.execute("select source, createdAt from ai_code_hashes"))
        finally:
            con.close()

    def completions(self) -> None:
        """Tab edits the hook log caught are completions. Where Tab may have
        written what nothing caught, a window instead: tracker rows no Tab
        hook record accounts for, or, when the tracker was missing or could
        not be read, a Cursor window on this folder whose editor hooks never
        ran. A tracker read with no Tab row here says Tab was not used: the
        editor hooks stay silent then too. Claude-format and cursor-agent
        records say nothing here: neither ever carries Tab."""
        tabs = [h for h in self.hooks if h.get("event") == "tab_edit"]
        for h in tabs:
            src = h["tool_input"] if isinstance(h.get("tool_input"), dict) else {}
            if CLIP in str(src.get("edits")):
                continue                              # clipped: which lines Tab wrote is not whole
            for e in _edits(src.get("edits")):
                if isinstance(e.get("new_string"), str) and e["new_string"]:
                    self.out["completions"].append({"t": _num(h.get("t")), "path": _s(src.get("file_path")),
                                                    "text": e["new_string"], "tool": self.name, "source": "tab"})
        seen = [_num(h.get("t")) for h in tabs if _num(h.get("t")) is not None]
        rows = [self.when(c / 1000) for s, c in self.tracked if s == "tab" and _num(c) is not None]
        missed = [t for t in rows if t is not None and not any(abs(t - x) <= TAB_SLACK for x in seen)]
        if missed:
            self.out["completion_windows"].append({
                "tool": self.name, "start": min(missed), "end": max(missed),
                "what": f"Cursor Tab wrote {len(missed)} line(s) the hook log did not record"})
        editor_hooks = [h for h in self.hooks if not h.get("via") and (
            h.get("event") == "tab_edit" or re.match(r"^\d{1,3}\.\d+", str(h.get("tool_version") or "")))]
        meta = _json_file(self.logs / "collect.json")
        tracker = _obj(meta.get("tracker")) if isinstance(meta, dict) else {}
        read = bool(self.tracked) or tracker.get("read") is True     # an older kit saved only the rows
        if (self.editor_chats or self.windows) and not editor_hooks and not read:
            self.out["completion_windows"].append({
                "tool": self.name, "start": None, "end": None,
                "what": "Cursor's editor hooks did not run: Tab edits were not recorded"})

    def coverage(self) -> None:
        extra = [p for p in self.logs.rglob("*") if p.is_file() and p not in self.read and p.name != "collect.json"
                 and not p.name.endswith(("-wal", "-shm", "-journal"))]
        if extra:
            self.issue(f"{len(extra)} file(s) not read: " + ", ".join(self.rel(p) for p in sorted(extra)[:3]))
        if self.counts["txt"]:
            self.issue(f"{self.counts['txt']} .txt transcript(s) not parsed")
        meta = _json_file(self.logs / "collect.json")
        versions = [h.get("tool_version") for h in sorted(self.hooks, key=lambda h: _num(h.get("t")) or 0)
                    if _s(h.get("tool_version"))]
        undated = sum(1 for e in self.out["events"] if e["kind"] == "tool_call" and e.get("t") is None
                      and e.get("op") in (base.EDIT, base.COMMAND) and e.get("actor") != "candidate")
        cov = self.out["coverage"]
        cov.update(format="ok" if not self.issues else "unrecognised: " + "; ".join(self.issues),
                   version=versions[-1] if versions else (meta.get("version") if isinstance(meta, dict) else None),
                   lost_bubbles=self.counts["lost_bubbles"], missing_content=self.counts["missing_content"],
                   undated_calls=undated, hook_only_sessions=sorted(self.hook_only))
        if self.tracked:
            cov["tracked_lines"] = dict(collections.Counter(s for s, _ in self.tracked))
        caveats = self.tr.setdefault("caveats", [])
        if self.counts["lost_bubbles"]:
            caveats.append(
                f"Cursor: {self.counts['lost_bubbles']} message(s) a conversation lists are gone from its store "
                "(editing an earlier prompt or restoring a checkpoint deletes the later ones): the hook log is "
                "the record of those turns")
        if self.counts["missing_content"]:
            caveats.append(
                f"Cursor: {self.counts['missing_content']} agent edit(s) whose text is not in the store (the "
                "collector's size limit, or Cursor kept no file text): lines they wrote cannot be matched to them")
        if undated:
            caveats.append(
                f"Cursor: {undated} agent call(s) have no time in any record (no store time, hook record or dated "
                "neighbour): provenance cannot place them; read them by hand")


class Cursor(base.Adapter):
    name = "cursor"
    title = "Cursor"

    def detect(self) -> str | None:
        for pj in app_roots():
            if pj.is_file():
                v = _json_file(pj)
                return str(v["version"]) if isinstance(v, dict) and v.get("version") else "installed"
        for d in cli_roots():
            vs = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
            if vs:
                return f"cursor-agent {vs[-1]}"
        return None

    def hook_files(self, python: str, hook_script: str, repo_root: str | None = None) -> dict[str, str]:
        """Cursor runs project hooks, in the editor and in cursor-agent, from the
        folder that holds .cursor/, so the build-time relative script path is
        right before `interview setup` makes it absolute. Not $CURSOR_PROJECT_DIR:
        in a multi-root window that is the first folder, which need not be this one.
        Exit 2 denies a call or a prompt, and python exits 2 when the script
        is gone (the folder moved since setup), so the command never fails."""
        hooks = {step: [{"command": f'"{python}" "{hook_script}" --tool {self.name} {event} || true', "timeout": 5}]
                 for step, event in HOOKS}
        return {".cursor/hooks.json": json.dumps({"version": 1, "hooks": hooks}, indent=2) + "\n"}

    def mcp_files(self, python: str, mcp_script: str, repo_root: str | None = None) -> dict[str, str]:
        """Cursor interpolates ${workspaceFolder} in mcp.json; the defaults use that."""
        from .. import configs
        script, repo = (mcp_script, repo_root) if repo_root else \
            (mcp_script if os.path.isabs(mcp_script) else "${workspaceFolder}/" + mcp_script, "${workspaceFolder}")
        return {".cursor/mcp.json": json.dumps({"mcpServers": {configs.SERVER: {
            "command": python, "args": [script], "env": {"INTERVIEW_REPO": repo}}}}, indent=2) + "\n"}

    def from_hook(self, rec: dict) -> dict | None:
        """preToolUse's Write carries the whole new file; afterFileEdit only the
        changed hunks; beforeShellExecution has no tool name, and its cwd is
        the record's own."""
        src = dict(rec["tool_input"]) if isinstance(rec.get("tool_input"), dict) else {}
        if rec.get("event") == "file_edit":
            path = _s(src.get("file_path"))
            return {"name": "afterFileEdit", "input": {"file_path": path}, "op": base.EDIT,
                    "writes": {path: _new_text(src.get("edits"))} if path else None, "command": None}
        if rec.get("event") != "tool_use":
            return None
        name = _s(rec.get("tool_name")) or ""
        if not name and (rec.get("hook_event_name") == "beforeShellExecution" or src.get("command")):
            name = "Shell"
        op, writes, command, cwd = _model_call(name, src)
        if op == base.COMMAND:
            cwd = cwd or _s(rec.get("cwd"))
        return {"name": name or "?", "input": src, "op": op, "writes": writes, "command": command, "cwd": cwd}

    def cross_checkable(self, rec: dict) -> bool:
        """Cursor gives an MCP call's hooks a fresh random tool_use_id, so no
        stored call ever matches one. Its payloads name the tool MCP:<tool>,
        through .claude/settings.json too (only the matcher is translated);
        mcp__<server>__<tool>, Claude's form, is taken the same way in case a
        version sends that."""
        return not str(rec.get("tool_name") or "").startswith(("MCP:", "mcp__"))

    def parse(self, tr: dict, logs: pathlib.Path, bad) -> dict:
        r = _Reader(self.name, tr, logs, bad)

        def guard(fn, p: pathlib.Path, *args) -> None:
            """A file in a shape nobody foresaw costs that file, never the rest."""
            try:
                fn(p, *args)
            except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError, KeyError) as e:
                r.read.add(p)
                r.issue(f"{r.rel(p)} not readable ({type(e).__name__}: {str(e)[:120]})")
        if logs.is_dir():
            eds = {p.parents[1] for p in logs.glob("editor*/globalStorage/state.vscdb")}
            eds |= {p.parents[2] for p in logs.glob("editor*/workspaceStorage/*/state.vscdb")}
            for d in sorted(eds):
                guard(r.editor, d)
            for f in sorted(logs.glob("*/state.json")):
                guard(r.legacy, f)
            for db in sorted(logs.glob("chats/*/*/store.db")) + sorted(logs.glob("acp-sessions/*/store.db")):
                guard(r.cli, db)
            for f in sorted(logs.glob("projects/*/agent-transcripts/**/*")):
                if not f.is_file():
                    continue
                r.read.add(f)
                if _enc(f.stem) in r.seen or f.stem in r.seen:
                    continue                            # a store holds this conversation
                if f.suffix == ".jsonl":
                    guard(r.transcript, f, f.parent.name == "subagents")
                elif f.suffix == ".txt" and not f.with_suffix(".jsonl").exists():
                    r.counts["txt"] += 1
            db = logs / "ai-tracking" / "ai-code-tracking.db"
            if db.exists():
                guard(r.tracking, db)
            r.read.add(logs / "collect.json")
        r.file_edits()
        r.completions()
        r.coverage()
        return r.out

    def collect(self, repo: pathlib.Path, out: pathlib.Path, save) -> tuple[list[str], list[str]]:
        """This session's work in this repository, and nothing else: see the
        module docstring for what leaves the machine."""
        root = pathlib.Path(repo).resolve()
        c = _Collector(root, save)
        # Cursor names a folder's chats/ and projects/ entries after the path
        # as it was opened, symlinks and all (Node's path.resolve)
        c.names = sorted({str(root), os.path.abspath(str(repo))})
        if c.window is None:
            shutil.rmtree(c.tmp, ignore_errors=True)
            return [], [("Cursor: no session start in .interview/recorder.json, so nothing was taken; "
                         "if you used Cursor, export the chat yourself")]
        try:
            for part in (c.editor, c.cli, c.transcripts, c.tracking):
                try:
                    part()
                except _Skip as e:
                    c.skipped.append(str(e))
                except Exception as e:           # one store in a shape nobody foresaw costs that store only
                    c.skipped.append(f"Cursor ({part.__name__}): could not read it ({type(e).__name__}: {e})")
        finally:
            shutil.rmtree(c.tmp, ignore_errors=True)
        n = c.n
        bits = [f"{n['chats']} conversation(s)" if n["chats"] else "",
                f"{n['subagents']} subagent(s)" if n["subagents"] else "",
                f"{n['cli']} CLI chat(s)" if n["cli"] else "",
                f"{n['transcripts']} transcript file(s)" if n["transcripts"] else "",
                f"{n['lines']} tracked line(s)" if n["lines"] else "",
                f"the state of {n['windows']} window(s) on this folder" if n["windows"] else ""]
        found = [f"{self.title}: " + ", ".join(b for b in bits if b)] if any(bits) else []
        if found:
            try:
                version = self.detect()
            except Exception:                   # a version is a nicety; what was found stands
                version = None
            meta = {"version": version, "window": c.window, "composers": c.how, "excluded": dict(c.excluded),
                    "tracker": c.tracker}
            if c.content_skipped:
                meta["content_skipped"] = c.content_skipped
            save("", "collect.json", meta)
        elif c.installed:
            c.skipped.append("Cursor: installed, but nothing for this folder. If you used it, export the chat yourself.")
        return found, c.skipped


class _Skip(Exception):
    """A store this collector will not copy; the message says why, for the candidate."""


class _Collector:
    """save(sub, name, data) writes agent_logs/cursor/<sub>/<name>; it returns
    None past the collector's size budget."""

    def __init__(self, root: pathlib.Path, save):
        self.root, self.save = root, save
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="ivkit-cursor-"))
        self.window = self._window()
        self.skipped: list[str] = []
        self.how: dict[str, str] = {}
        self.scope: dict[str, str] = {}           # composer id -> "workspace" or "path" (another folder's chat)
        self.known: set = set()                  # conversation ids this folder's own records name
        self.excluded: collections.Counter = collections.Counter()
        self.n: collections.Counter = collections.Counter()
        self.content_skipped = 0
        self.installed = False
        self.names = [str(root)]
        self.tracker: dict = {"found": False}

    def _window(self) -> list | None:
        """The session on this machine's clock, which is the clock Cursor stamps
        its own records with."""
        try:
            started = float(json.loads((self.root / ".interview" / "recorder.json").read_text())["started"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return [started - WINDOW_SLACK, time.time() + 60]

    def recent(self, *ms_) -> bool:
        """Any of these machine times (epoch ms) is inside the session. A later
        one counts too: a clock set wrong must not lose the session's work."""
        t = [x / 1000 for x in ms_ if _num(x) is not None]
        return bool(t) and max(t) >= self.window[0]

    def recent_file(self, *ps: pathlib.Path) -> bool:
        return any(p.exists() and p.stat().st_mtime >= self.window[0] for p in ps)

    def saved(self, sub: str, name: str, data) -> bool:
        if self.save(sub, name, data) is not None:
            return True
        msg = "Cursor: the collector's size budget was reached; some of Cursor's records were not saved. Tell the panel."
        if msg not in self.skipped:
            self.skipped.append(msg)
        return False

    def snapshot(self, db: pathlib.Path) -> tuple[sqlite3.Connection, pathlib.Path]:
        """A consistent copy of a store the tool may be writing right now,
        and never a file created beside it. A store open in WAL mode (its -wal
        and -shm both there) or in a rollback journal: SQLite's backup from a
        read-only connection, which reads through the log in one read
        transaction. A WAL store missing either: closed, and a read-only
        connection would create them in the tool's directory, so its files
        are copied as they are, which with no writer is whole, and only the
        copy is opened. Never the live file opened immutable, which would
        miss the log."""
        wal = db.with_name(db.name + "-wal")
        size = sum(p.stat().st_size for p in (db, wal) if p.exists())
        if size > STORE_CAP:
            raise _Skip(f"Cursor: {db.parent.name}/{db.name} is too large to copy ({size / 1e9:.1f} GB, over "
                        f"{STORE_CAP / 1e9:.0f} GB); if you used Cursor, export the chat yourself")
        if _free(self.tmp) < size + HEADROOM:
            raise _Skip(f"Cursor: not enough free disk space to copy {db.parent.name}/{db.name} "
                        f"({size / 1e6:.0f} MB); free some space and collect again")
        dest = pathlib.Path(tempfile.mkdtemp(dir=self.tmp)) / db.name
        with open(db, "rb") as f:
            rollback = f.read(20)[18:20] == b"\x01\x01"      # the header's file format versions: 2 is WAL
        if rollback or (wal.exists() and db.with_name(db.name + "-shm").exists()):
            src = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
            try:
                out = sqlite3.connect(dest)
                try:
                    src.backup(out)
                finally:
                    out.close()
            finally:
                src.close()
        else:
            for side in ("", "-wal"):
                if db.with_name(db.name + side).exists():
                    shutil.copy2(db.with_name(db.name + side), dest.with_name(dest.name + side))
        return sqlite3.connect(dest), dest

    def write(self, src: sqlite3.Connection, rows: dict) -> pathlib.Path:
        """A new SQLite file holding only `rows` ({table: (columns, tuples)}),
        in the source's own table definitions, the columns named."""
        dest = pathlib.Path(tempfile.mkdtemp(dir=self.tmp)) / "copy.db"
        con = sqlite3.connect(dest)
        try:
            for table, (cols, rs) in rows.items():
                sql = src.execute("select sql from sqlite_master where type = 'table' and name = ?", (table,)).fetchone()
                if not sql:
                    continue
                con.execute(sql[0])
                if rs:
                    names = ", ".join('"%s"' % c.replace('"', '""') for c in cols)
                    con.executemany(f'insert into "{table}" ({names}) values ({", ".join("?" * len(cols))})', rs)
            con.commit()
        finally:
            con.close()
        return dest

    # ------------------------------------------------ the editor

    def editor(self) -> None:
        for k, ud in enumerate(user_data_roots()):
            user = ud / "User"
            if not user.is_dir():
                continue
            self.installed = True
            name = "editor" if k == 0 else f"editor-{k + 1}"
            wcons: dict = {}
            try:
                for wd in sorted((user / "workspaceStorage").glob("*")):
                    if (wd / "state.vscdb").exists() and _under(_folder(wd), self.root):
                        try:
                            wcons[wd] = self.snapshot(wd / "state.vscdb")[0]
                        except _Skip as e:
                            self.skipped.append(str(e))
                listed = {}
                for con in wcons.values():
                    r = con.execute("select value from ItemTable where key = 'composer.composerData'").fetchone()
                    for h in (_obj(r[0]).get("allComposers") or []) if r else []:
                        if isinstance(h, dict) and _s(h.get("composerId")):
                            listed[h["composerId"]] = h
                matched: set = set()
                g = user / "globalStorage" / "state.vscdb"
                if g.exists():
                    try:
                        con = self.snapshot(g)[0]
                    except _Skip as e:
                        self.skipped.append(str(e))
                    else:
                        try:
                            matched = self._global(con, name, {wd.name for wd in wcons}, listed)
                        finally:
                            con.close()
                for wd, con in wcons.items():
                    self.workspace(con, wd, name, matched)
            finally:
                for con in wcons.values():
                    con.close()

    def workspace(self, con, wd: pathlib.Path, name: str, matched: set) -> None:
        """A window on this folder: its index, reduced to the kept chats, and
        its Cmd-K generations from the session. Not aiService.prompts: those
        carry no time, and the hooks keep Cmd-K's prompts with theirs."""
        keep, kept = [], []
        r = con.execute("select value from ItemTable where key = 'composer.composerData'").fetchone()
        if r:
            kept = [h for h in _obj(r[0]).get("allComposers") or []
                    if isinstance(h, dict) and h.get("composerId") in matched]
            keep.append(("composer.composerData", json.dumps({"allComposers": kept})))
        r = con.execute("select value from ItemTable where key = 'aiService.generations'").fetchone()
        gens = _loads(r[0]) if r else None
        gens = [g for g in gens if isinstance(g, dict) and self.recent(g.get("unixMs"))] if isinstance(gens, list) else []
        if gens:
            keep.append(("aiService.generations", json.dumps(gens)))
        if not kept and not gens and not self.recent_file(wd / "state.vscdb", wd / "state.vscdb-wal"):
            self.excluded["windows used only before the session"] += 1
            return
        dest = f"{name}/workspaceStorage/{wd.name}"
        if self.saved(dest, "state.vscdb", self.write(con, {"ItemTable": (("key", "value"), keep)})) and \
                self.saved(dest, "workspace.json", wd / "workspace.json"):
            self.n["windows"] += 1

    def _global(self, con, name: str, ws_ids: set, listed: dict) -> set:
        """The editor-wide store, reduced to this folder's chats of the session.
        A chat is this folder's when it sits in a window on this folder (its
        header's folder or workspace, or the folder's own index), or, in
        another folder, when a tool call of the session worked here; its
        subagents follow it. Of each only the session's messages are kept, of
        another folder's chat only the calls that worked here, and file text
        only for files here."""
        tables = _tables(con)
        if "cursorDiskKV" not in tables:
            return set()
        item = lambda k: (lambda r: r[0] if r else None)(con.execute(
            "select value from ItemTable where key = ?", (k,)).fetchone()) if "ItemTable" in tables else None
        trows, tcols = None, []
        if "composerHeaders" in tables:
            cur = con.execute("select * from composerHeaders")
            tcols = [d[0] for d in cur.description]
            trows = [dict(zip(tcols, r)) for r in cur]
        key = item("composer.composerHeaders")
        headers, _ = _index([r.get("value") for r in trows] if trows is not None else None, key,
                            [{"allComposers": list(listed.values())}])
        get = lambda k: (lambda r: r[0] if r else None)(con.execute(
            "select value from cursorDiskKV where key = ?", (k,)).fetchone())
        bounds = lambda p: (p, p[:-1] + chr(ord(p[-1]) + 1))
        rows = lambda p: con.execute("select key, value from cursorDiskKV where key >= ? and key < ?", bounds(p)).fetchall()
        ids = set(headers) | {k[len("composerData:"):] for (k,) in con.execute(
            "select key from cursorDiskKV where key >= ? and key < ?", bounds("composerData:"))}
        cache: dict = {}

        def load(c: str) -> dict:
            if c not in cache:
                cache[c] = _obj(get("composerData:" + c))
            return cache[c]

        def here(c: str) -> str | None:
            h = headers.get(c) or {}
            ht = (h.get("lastUpdatedAt"), h.get("createdAt"))
            ws = c in listed or _under(_fs(h), self.root) or _s(_obj(h.get("workspaceIdentifier")).get("id")) in ws_ids
            if any(_num(x) is not None for x in ht) and not self.recent(*ht):
                if ws:
                    self.excluded["chats active only before the session"] += 1
                return None
            d = load(c)
            ws = ws or _under(_fs(d), self.root)
            dt = ht + (d.get("lastUpdatedAt"), d.get("createdAt"))
            active = self.recent(*dt) if any(_num(x) is not None for x in dt) else \
                any(self.recent(_bubble_ms(_obj(v))) for _, v in rows(f"bubbleId:{c}:"))
            if active and ws:
                return "workspace"
            if active and any(_under(p, self.root) for _, v in rows(f"bubbleId:{c}:") for p in _tool_paths(_obj(v))):
                return "path"
            if ws:
                self.excluded["chats active only before the session"] += 1
            cache.pop(c, None)
            return None
        pending = set()
        for c in sorted(ids):
            if _is_sub(c, (headers.get(c) or {}).get("subagentInfo")) is not None:
                pending.add(c)                        # decided with its parent
                continue
            how = here(c)
            if how:
                self.how[c], self.scope[c] = how, how
        grew = True
        while grew and pending:                       # subagents follow their parent, to a fixpoint
            grew = False
            kids = {}
            for p in list(self.how):
                for f in ("subagentComposerIds", "subComposerIds"):
                    v = load(p).get(f)
                    kids.update({s: p for s in v if isinstance(s, str)} if isinstance(v, list) else {})
            for c in sorted(pending):
                parent = _is_sub(c, (headers.get(c) or {}).get("subagentInfo"))
                if parent not in self.how:
                    parent = _is_sub(c, load(c).get("subagentInfo")) or kids.get(c)
                if parent in self.how:
                    self.how[c], self.scope[c] = "subagent", self.scope[parent]
                    pending.discard(c)
                    grew = True
        for c in sorted(pending):                     # a subagent whose parent is not this folder's
            how = here(c)
            if how:
                self.how[c], self.scope[c] = how, how
        mine = [c for c in sorted(ids) if c in self.how]
        if not mine:
            return set()
        kv, refs = [], []
        for c in mine:
            kv += self._chat(c, load(c), rows(f"bubbleId:{c}:"), refs)
        size = 0
        for k in sorted(set(refs)):
            v = get(k)
            if v is None:
                continue
            n = len(v) if isinstance(v, (str, bytes)) else 0
            if size + n > CONTENT_CAP:
                self.content_skipped += 1             # parse counts the missing text
                continue
            size += n
            kv.append((k, v))
        if self.content_skipped:
            self.skipped.append(f"Cursor: the text of {self.content_skipped} file version(s) was left out, past the "
                                f"{CONTENT_CAP // 1024 // 1024} MB of file text this collector keeps")
        header = lambda h, c: {k: v for k, v in _keep(h, HEADER_KEEP).items()
                               if not (self.scope.get(c) == "path" and k in ELSEWHERE)}
        items = []
        if key is not None:
            items.append(("composer.composerHeaders", json.dumps({"allComposers": [
                header(h, h["composerId"]) for h in _obj(key).get("allComposers") or []
                if isinstance(h, dict) and h.get("composerId") in self.how]})))
        flag = item("composer.composerHeaders.migratedToTable")
        if flag is not None:
            items.append(("composer.composerHeaders.migratedToTable", flag))
        out = {"ItemTable": (("key", "value"), items), "cursorDiskKV": (("key", "value"), kv)}
        if trows is not None:
            keep = []
            for r in trows:
                c = r.get("composerId")
                if c not in self.how:
                    continue
                r = dict(r)
                if "value" in r:
                    r["value"] = json.dumps(header(_obj(r["value"]), c))
                if self.scope[c] == "path" and "workspaceId" in r:
                    r["workspaceId"] = None
                keep.append(tuple(r[x] for x in tcols))
            out["composerHeaders"] = (tcols, keep)
        if not self.saved(f"{name}/globalStorage", "state.vscdb", self.write(con, out)):
            return set()
        self.n["subagents"] += sum(self.how[c] == "subagent" for c in mine)
        self.n["chats"] += sum(self.how[c] != "subagent" for c in mine)
        self.known |= set(mine)
        return set(mine)

    def _chat(self, c: str, d: dict, stored: list, refs: list) -> list:
        """One chat's rows, reduced: the session's messages, and for another
        folder's chat only the calls that worked here, stripped to what the
        parser reads. `refs` gets the file text the kept edits name."""
        sc = self.scope[c]
        stored = dict(stored)
        inline = d.get("conversationMap") if isinstance(d.get("conversationMap"), dict) else {}
        heads = d.get("fullConversationHeadersOnly")
        items = []
        for h in heads if isinstance(heads, list) else []:
            bid = _s(h.get("bubbleId")) if isinstance(h, dict) else None
            if not bid:
                continue
            k = f"bubbleId:{c}:{bid}"
            raw = stored.get(k)
            b = _obj(raw) if raw is not None else inline.get(bid)
            items.append((h, bid, k, raw, b if isinstance(b, dict) else None))
        # a message with no time of its own has its neighbours'
        own = [_bubble_ms(b) for *_, b in items]
        later = [None] * len(own)
        for i in range(len(own) - 1, -1, -1):
            later[i] = own[i] if own[i] is not None else (later[i + 1] if i + 1 < len(own) else None)
        last, start = None, self.window[0] * 1000
        out, heads_kept, inline_kept = [], [], {}
        for (h, bid, k, raw, b), t, t2 in zip(items, own, later):
            last = t if t is not None else last
            t = t if t is not None else last if last is not None else t2 if t2 is not None else \
                _num(d.get("lastUpdatedAt")) or _num(d.get("createdAt"))
            if t is not None and t < start:
                self.excluded["messages from before the session"] += 1
                continue
            if sc == "path":
                if b is None or not any(_under(p, self.root) for p in _tool_paths(b)):
                    self.excluded["messages of a chat in another folder"] += 1
                    continue
                b = _keep(b, BUBBLE_KEEP)
            heads_kept.append(h)
            if raw is not None:
                out.append((k, raw if sc == "workspace" else json.dumps(b)))
            elif b is not None:
                inline_kept[bid] = b
            if b is not None:
                refs += self._refs(b, sc)
        nd = _keep(d, COMPOSER_KEEP)
        nd["fullConversationHeadersOnly"] = heads_kept
        if inline:
            nd["conversationMap"] = inline_kept
        for x in ELSEWHERE if sc == "path" else ():
            nd.pop(x, None)
        out.append(("composerData:" + c, json.dumps(nd)))
        return out

    def _refs(self, b: dict, scope: str) -> list:
        """The composer.content rows an edit of a file here names: an absolute
        path under the repository, or a relative one in a chat of this folder."""
        tf = _obj(b.get("toolFormerData"))
        params, raw = _obj(tf.get("params")), _obj(tf.get("rawArgs"))
        if _op(tf.get("tool") if _num(tf.get("tool")) is not None else None, _s(tf.get("name")) or "") != base.EDIT:
            return []
        path = _first(params.get("relativeWorkspacePath"), raw.get("path"), raw.get("file_path"))
        if not (_under(path, self.root) or (scope == "workspace" and path and not os.path.isabs(path))):
            return []
        res = _obj(tf.get("result"))
        return [res[x] for x in ("beforeContentId", "afterContentId")
                if _s(res.get(x)) and res[x].startswith("composer.content.")]

    # ------------------------------------------------ cursor-agent, the transcripts, line records

    def cli(self) -> None:
        """Chats whose folder is this one (its md5 names the directory, or
        meta.json's cwd is here) and that were active in the session. Taken
        whole, without the chat's blob encryption key."""
        cfg = config_dir()
        mine = {hashlib.md5(n.encode()).hexdigest() for n in self.names}
        for d in sorted(cfg.glob("chats/*/*")) + sorted(cfg.glob("acp-sessions/*")):
            if not d.is_dir():
                continue
            self.installed = True
            meta = _json_file(d / "meta.json")
            meta = meta if isinstance(meta, dict) else {}
            if not (d.parent.name in mine or _under(_s(meta.get("cwd")), self.root)):
                continue
            times = (meta.get("updatedAtMs"), meta.get("createdAtMs"))
            if not (self.recent(*times) if any(_num(x) is not None for x in times) else
                    self.recent_file(d / "store.db", d / "store.db-wal")):
                self.excluded["CLI chats active only before the session"] += 1
                continue
            dest = d.relative_to(cfg).as_posix()
            if (d / "store.db").exists():
                try:
                    con, flat = self.snapshot(d / "store.db")
                except _Skip as e:
                    self.skipped.append(str(e))
                    continue
                try:
                    r = con.execute("select value from meta where key = '0'").fetchone()
                    m = json.loads(bytes.fromhex(r[0]).decode("utf-8")) if r else None
                    if isinstance(m, dict) and "blobEncryptionKey" in m:
                        m.pop("blobEncryptionKey")
                        con.execute("update meta set value = ? where key = '0'", (json.dumps(m).encode().hex(),))
                        con.commit()
                    con.execute("PRAGMA journal_mode=DELETE")
                    con.execute("VACUUM")                 # the key's old page goes with it
                except (sqlite3.Error, ValueError, TypeError):
                    pass                                  # parse says what it cannot read
                finally:
                    con.close()
                if not self.saved(dest, "store.db", flat):
                    continue
            if (d / "meta.json").exists() and not self.saved(dest, "meta.json", d / "meta.json"):
                continue
            self.n["cli"] += 1
            self.known.add(d.name)

    def hook_ids(self) -> tuple[set, set]:
        """The Cursor conversation ids and transcript paths this folder's own
        hook log names."""
        ids, files = set(), set()
        try:
            with open(self.root / ".interview" / "transcript.jsonl", "rb") as f:
                for raw in f:
                    try:
                        r = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("tool") == "cursor":
                        ids.add(_s(r.get("session_id")))
                        files.add(_s(r.get("transcript_path")))
        except OSError:
            pass                                          # no hook log: nothing names a conversation
        return ids - {None}, files - {None}

    def transcripts(self) -> None:
        """projects/<slug> of this folder, or a directory whose trust file names
        a folder here. A slug is shared by take-home, take_home and take.home,
        so unless the trust file names this folder, only conversations this
        folder's own records name are taken."""
        base_ = data_dir() / "projects"
        slugs = {slug(n) for n in self.names}
        hooked, files = self.hook_ids()
        for pd in sorted(base_.glob("*")) if base_.is_dir() else []:
            if not pd.is_dir():
                continue
            self.installed = True
            trust = _json_file(pd / ".workspace-trusted")
            trusted = isinstance(trust, dict) and _under(_s(trust.get("workspacePath")), self.root)
            if pd.name not in slugs and not trusted:
                continue
            for f in sorted((pd / "agent-transcripts").rglob("*")):
                if not f.is_file() or f.suffix not in (".jsonl", ".txt"):
                    continue
                if f.stat().st_mtime < self.window[0]:
                    self.excluded["transcripts from before the session"] += 1
                    continue
                parent = f.parent.parent.name if f.parent.name == "subagents" else None
                if not trusted and not ({f.stem, parent} & (self.known | hooked) or str(f) in files):
                    self.excluded["transcripts not shown to be this folder's"] += 1
                    continue
                if self.saved(f"projects/{pd.name}/{f.parent.relative_to(pd).as_posix()}", f.name, f):
                    self.n["transcripts"] += 1

    def placed(self, name) -> bool:
        """A tracker row's file is in this repository: an absolute path here,
        or a relative one naming a file that is here."""
        n = _s(name)
        if not n:
            return False
        if os.path.isabs(n):
            return _under(n, self.root)
        try:
            p = (self.root / n).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return p.is_file() and self.root in p.parents

    def tracking(self) -> None:
        """Rows made during the session for files in this repository. A row
        whose file cannot be placed is left behind. collect.json says whether
        the tracker was there and read, and how many rows it had here, zero
        included: a tracker read with no Tab row says Tab was not used."""
        db = tracking_db()
        if not db.exists():
            return
        self.tracker = {"found": True, "read": False}
        con = self.snapshot(db)[0]
        try:
            if "ai_code_hashes" not in _tables(con):
                return
            cur = con.execute("select * from ai_code_hashes where createdAt >= ? and createdAt <= ?",
                              (self.window[0] * 1000, self.window[1] * 1000))
            cols = [d[0] for d in cur.description]
            if "fileName" not in cols:
                return
            at = cols.index("fileName")
            rows = [tuple(r) for r in cur if self.placed(r[at])]
            tab = sum(r[cols.index("source")] == "tab" for r in rows) if "source" in cols else None
            self.tracker.update(read=True, rows=len(rows), tab=tab)
            if rows and self.saved("ai-tracking", "ai-code-tracking.db", self.write(con, {"ai_code_hashes": (cols, rows)})):
                self.n["lines"] += len(rows)
        finally:
            con.close()


ADAPTER = Cursor()
