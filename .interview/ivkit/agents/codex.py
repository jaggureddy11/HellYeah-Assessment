"""Codex: the CLI, the desktop app (ChatGPT.app, bundle id com.openai.codex)
and the VS Code extension. All three write one rollout file per thread under
$CODEX_HOME (~/.codex) and read project hooks from .codex/hooks.json.

What a rollout holds. Each line is {timestamp, ordinal, type, payload}. The
record of what happened is the `event_msg` `item_completed` items:
UserMessage, AgentMessage, CommandExecution, FileChange and McpToolCall, with
started_at_ms and completed_at_ms. Their ids are the hooks' tool_use_id. In
code mode (the desktop app, and the CLI it bundles) the model's only tool call
is one JavaScript `exec`; the commands and edits it makes exist only as these
items. Older files have no items and are read from their `response_item`
function calls, as before.

A fork, a revert and a subagent each get their own file. A fork or revert
refers to its parent's prefix (history_base), or starts with records copied
from it; a subagent's file starts with records inherited from its parent.
Nothing is counted twice.

Only collect, hook_files, mcp_files, detect and from_hook run in the
candidate's kit, which has no trace or review: those two are imported by
parse alone.

Facts this relies on are from openai/codex rust-v0.158.0; see the design,
vibe-interview/specs/2026-09-29-multi-tool-capture-design.md section 3.1.
"""
from __future__ import annotations
import json, math, os, pathlib, re, urllib.parse

from . import base
from .. import configs

MODES = ("legacy", "paginated")               # session_meta.history_mode this adapter knows
READABLE = ("message", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
            "reasoning")
CALL_ITEMS = ("CommandExecution", "FileChange", "McpToolCall")
SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "fish")
COMMANDS = ("exec_command", "shell", "shell_command", "container.exec", "exec")
NAME = re.compile(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-([0-9A-Za-z-]+?)(?:_([0-9A-Za-z-]+))?\.jsonl(?:\.zst)?$")
WINDOW_SLACK = 600.0       # seconds around the session window, for collect
# The desktop app is ChatGPT.app now; "ChatGPT Classic" (com.openai.chat) is the old chat app.
APPS = (pathlib.Path("/Applications/ChatGPT.app"), pathlib.Path.home() / "Applications" / "ChatGPT.app")
BUNDLE_ID = "com.openai.codex"
BUNDLED_CLI = pathlib.Path("Contents/Resources/codex-cli/bin/codex")


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _num(v) -> float | None:
    """A finite number, or None: a rollout is the candidate's file and may hold anything."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def _epoch(v) -> float | None:
    """A rollout's UTC stamp, 2026-09-29T09:15:24.209Z. collect runs in the
    candidate's kit, which has no trace.epoch."""
    import datetime
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", str(v or "").strip())
    if not m:
        return None
    frac = "." + (m.group(2) + "000000")[:6] if m.group(2) else ""
    tz = "+00:00" if m.group(3) in (None, "Z") else m.group(3)
    try:
        return datetime.datetime.fromisoformat(m.group(1) + frac + tz).timestamp()
    except (ValueError, OverflowError):
        return None


def _ms(v) -> float | None:
    """Epoch milliseconds to seconds. Old files wrote 0 for unknown."""
    v = _num(v)
    return v / 1000.0 if v is not None and v > 0 else None


def _secs(v) -> float | None:
    """A Rust Duration as serde writes it: {secs, nanos}."""
    v = _d(v)
    secs, nanos = _num(v.get("secs")), _num(v.get("nanos"))
    return None if secs is None else secs + (nanos or 0) / 1e9


def _text(content) -> str:
    """An item's or a response message's text parts, joined the same way for both."""
    return "".join(str(b.get("text", "")) for b in content if isinstance(b, dict)) if isinstance(content, list) \
        else ""


def _unwrap(argv) -> str:
    """The script a shell was asked to run. Codex runs every command as
    `<shell> -lc <script>`, and its hooks report the script."""
    if isinstance(argv, list):
        if len(argv) == 3 and argv[1] in ("-lc", "-c") and os.path.basename(str(argv[0])) in SHELLS:
            return str(argv[2])
        return " ".join(str(a) for a in argv)
    return str(argv) if argv else ""


def _path(uri) -> str | None:
    s = str(uri or "")
    if not s.startswith("file://"):
        return s or None
    p = urllib.parse.unquote(s[len("file://"):])
    return p[1:] if re.match(r"^/[A-Za-z]:", p) else p          # file:///C:/x is C:/x


def _plus(diff: str) -> str:
    """What a unified diff put in the file: its + lines after the first hunk header."""
    body = "@@" not in diff
    out = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            body = True
        elif body and line.startswith("+"):
            out.append(line[1:])
    return "\n".join(out)


def _writes(changes: dict) -> dict:
    """FileChange.changes to {path: text written}. A delete writes nothing; a
    move writes to where the file went."""
    out = {}
    for path, ch in changes.items():
        ch = _d(ch)
        if ch.get("type") == "add":
            out[str(path)] = str(ch.get("content") or "")
        elif ch.get("type") == "update":
            out[str(ch.get("move_path") or path)] = _plus(str(ch.get("unified_diff") or ""))
    return out


def _patch_text(s: str) -> str:
    """apply_patch text, also when code mode wrapped it in a JS string literal."""
    if "*** Begin Patch" in s and s.count("\n") < 3 and "\\n" in s:
        s = s.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return s


def patch_writes(text: str) -> dict:
    """{path: + lines} of apply_patch text, also inside a JavaScript string."""
    out: dict = {}
    cur = None
    for line in _patch_text(str(text)).splitlines():
        m = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)$", line)
        if m:
            cur = out.setdefault(m.group(2).strip(), []) if m.group(1) != "Delete" else None
        elif line.startswith("*** Move to: ") and cur is not None:
            old = next(p for p, v in out.items() if v is cur)
            out[line[len("*** Move to: "):].strip()] = out.pop(old)
        elif line.startswith("*** ") and line.strip() != "*** End of File":   # that one ends a hunk, not the file
            cur = None
        elif cur is not None and line.startswith("+"):
            cur.append(line[1:])
    return {p: "\n".join(v) for p, v in out.items()}


def mcp_name(server, tool) -> str:
    """As Codex names an MCP tool to the model and to hooks."""
    return re.sub(r"[^A-Za-z0-9_]", "_", f"mcp__{server}__{tool}")


def _op(name: str, inp) -> str:
    if name == "apply_patch" or (name == "exec" and "*** Begin Patch" in str(inp)):
        return base.EDIT
    if name in COMMANDS:
        return base.COMMAND
    if name in ("spawn_agent", "Agent"):
        return base.AGENT
    return base.OTHER


def _command(inp) -> tuple[str | None, str | None]:
    """(command, cwd) from a shell call's arguments."""
    if isinstance(inp, dict):
        cwd = inp.get("workdir") or inp.get("cwd")
        return _unwrap(inp.get("cmd") or inp.get("command")) or None, cwd if isinstance(cwd, str) else None
    return (str(inp) or None), None


def _meta(f: pathlib.Path) -> dict:
    try:
        with f.open("rb") as fh:
            r = json.loads(fh.readline())
    except (OSError, ValueError, RecursionError):
        return {}
    r = _d(r)
    return {**_d(r.get("payload")), "_t": r.get("timestamp")} if r.get("type") == "session_meta" \
        else {"_t": r.get("timestamp"), "_none": True}


def rollout_id(f: pathlib.Path, meta: dict) -> str:
    """A revert's file carries a rollout id of its own after the thread id."""
    m = NAME.search(f.name)
    return (m.group(2) or m.group(1)) if m else str(meta.get("id") or f.stem)


def _subagent(meta: dict) -> tuple[str, dict] | None:
    src = meta.get("source")
    if not isinstance(src, dict) or "subagent" not in src:
        return None
    sub = src["subagent"]
    if not isinstance(sub, dict):
        return str(sub), {}
    spawn = _d(sub.get("thread_spawn"))
    kind = (meta.get("agent_role") or spawn.get("agent_role") or meta.get("agent_nickname")
            or spawn.get("agent_nickname") or next(iter(sub), "?"))
    return str(kind), spawn


def _inherited(meta: dict) -> int | None:
    """The ordinal below which a subagent's or fork's records are the ones
    it took from its parent, which are read in the parent's own file."""
    marks = [meta.get("subagent_history_start_ordinal") if _subagent(meta) else None,
             meta.get("forked_from_ordinal_exclusive")]
    marks = [m for m in marks if isinstance(m, int) and not isinstance(m, bool)]
    return max(marks) if marks else None


class _File:
    """One rollout, read once, its events kept in file order."""

    def __init__(self, a: "Codex", tr: dict, f: pathlib.Path, rel: str, meta: dict, run: dict, bad):
        from .. import trace                    # panel-only, like review: parse never runs in the kit
        from ..review import is_injected
        self.T, self.injected = trace, is_injected
        self.tr, self.rel, self.run, self.tool, self.bad = tr, rel, run, a.name, bad
        sub = _subagent(meta)
        self.sub = bool(sub)
        self.actor = f"subagent:{sub[0]}" if sub else a.name
        parent = (sub[1].get("parent_thread_id") or meta.get("parent_thread_id")) if sub else None
        # hooks send the root thread's id, which a subagent's session_id names
        self.session = str(meta.get("session_id") or parent or meta.get("id") or f.stem)
        # records a fork or subagent took from its parent come first, below this ordinal
        self.inherited = _inherited(meta)
        cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
        self.info = base.session_info(a.name, self.session, rel, self.actor, cwd=cwd)
        self.info.update(originator=meta.get("originator"), cli_version=meta.get("cli_version"), compactions=0,
                         forked_from=meta.get("forked_from_id"), history_base=None)
        if sub:
            self.info.update(type=sub[0], depth=sub[1].get("depth"), parent=parent)
        self.rows: list = []            # (line, event) for this file
        self.later: list = []           # response items, decided once every item is seen
        self.items: set = set()         # ids of this file's call items
        self.agent_texts: set = set()
        self.outputs: dict = {}         # call_id -> local time of its output
        self.issued: dict = {}          # call_id -> local time the model issued it
        self.readable = 0

    def time(self, local):
        return self.T.server_time(self.tr, local)

    def seen(self, t) -> None:
        if t is not None:
            self.info["first"] = t if self.info["first"] is None else min(self.info["first"], t)
            self.info["last"] = t if self.info["last"] is None else max(self.info["last"], t)

    def line(self, n: int, r: dict) -> None:
        if self.inherited is not None and isinstance(r.get("ordinal"), int) and r["ordinal"] < self.inherited:
            return                      # the parent's, in the parent's own file
        local = _num(r.get("timestamp")) if isinstance(r.get("timestamp"), (int, float)) \
            else self.T.epoch(r.get("timestamp"))
        self.seen(self.time(local))
        typ, p = r.get("type"), _d(r.get("payload"))
        pt = p.get("type")
        if typ == "turn_context" and isinstance(p.get("model"), str):
            self.info["models"].add(p["model"])
        elif typ == "compacted":
            self.info["compactions"] += 1            # replacement_history repeats earlier turns: not read
        elif typ == "event_msg" and pt == "item_completed" and isinstance(p.get("item"), dict):
            self.item(n, local, p, p["item"])
        elif typ == "event_msg" and pt == "user_message":
            self.prompt(n, local, str(p.get("message") or "").strip())
        elif typ == "response_item":
            self.readable += pt in READABLE
            if pt in ("function_call_output", "custom_tool_call_output"):
                self.outputs.setdefault(p.get("call_id"), local)
            elif pt in ("function_call", "custom_tool_call", "message"):
                if pt != "message":
                    self.issued.setdefault(p.get("call_id"), local)
                self.later.append((n, local, p))

    def prompt(self, n: int, local, text: str) -> None:
        t = self.time(local)
        if not text or self.injected(text):
            return
        if self.sub:                                    # a subagent's user turns come from its parent
            if self.info["brief"] is None:
                self.info["brief"] = {"t": t, "text": text, "ref": f"{self.rel}:{n}"}
                self.rows.append((n, base.event(t, self.tool, "brief", self.actor, text, f"{self.rel}:{n}",
                                                session=self.session)))
            return
        key = (self.session, self.T.norm(text))
        if any(k == key and t is not None and w is not None and abs(t - w) < 10 for k, w in self.run["prompts"]):
            return                      # a legacy file logs one turn twice, as an event and as an item
        self.run["prompts"].append((key, t))
        self.info["prompts"] += 1
        self.rows.append((n, base.prompt(t, local, self.tool, text, self.session, f"{self.rel}:{n}")))

    def item(self, n: int, local, p: dict, it: dict) -> None:
        kind, iid = it.get("type"), it.get("id")
        iid = iid if isinstance(iid, (str, int)) else None
        text = _text(it.get("content")) if kind == "AgentMessage" else ""
        # what this file holds, even where another file gave it first: its response items pair with it
        if kind in CALL_ITEMS:
            self.items.add(iid)
        elif kind == "AgentMessage":
            self.agent_texts.add(self.T.norm(text))
        if iid is not None and iid in self.run["ids"]:
            return                      # a copy of a record another file already gave
        if iid is not None:
            self.run["ids"].add(iid)
        start, done = _ms(p.get("started_at_ms")), _ms(p.get("completed_at_ms"))
        if kind == "UserMessage":
            text = " ".join(str(b.get("text", "")) for b in it.get("content") or []
                            if isinstance(b, dict) and b.get("text")).strip()
            self.prompt(n, start or done or local, text)
        elif kind == "AgentMessage":
            if text.strip():
                self.rows.append((n, base.agent_text(self.time(start or done or local), self.tool, self.actor,
                                                     text, f"{self.rel}:{n}", self.session)))
        elif kind in CALL_ITEMS:
            # 0.158 stamps some commands' start at their end: the duration says when they began
            dur = _secs(it.get("duration"))
            ran = [x for x in (start, (done - dur) if done is not None and dur is not None else None) if x is not None]
            start = min(ran) if ran else (self.issued.get(iid) or done)
            fields = self.call_fields(kind, it)
            if fields is not None:
                self.call(n, start or local, done, iid, **fields)

    def call_fields(self, kind: str, it: dict) -> dict | None:
        status = it.get("status")
        if kind == "CommandExecution":
            # applied False only when it never ran: a command that ran and failed may have written files
            command = _unwrap(it.get("command"))
            dur, code = _secs(it.get("duration")), it.get("exit_code")
            ran = (isinstance(code, int) and code != -1) or bool(dur)
            error = "declined" if status == "declined" else None if status != "failed" else \
                f"exit {code}" if ran and isinstance(code, int) else "failed" if ran else "failed before it ran"
            return dict(name="Bash", op=base.COMMAND, text=command, command=command, writes=None,
                        cwd=_path(it.get("cwd")), error=error,
                        applied=not (status == "declined" or (status == "failed" and not ran)),
                        input={"command": command, "argv": it.get("command"), "cwd": _path(it.get("cwd")),
                               "source": it.get("source"), "status": status, "exit_code": code},
                        actor="candidate" if it.get("source") == "user_shell" else self.actor)
        if kind == "FileChange":
            if not isinstance(it.get("changes"), dict):
                return None
            refused = status in ("failed", "declined")
            return dict(name="apply_patch", op=base.EDIT, text=", ".join(map(str, it["changes"])), command=None,
                        writes=_writes(it["changes"]), cwd=None, error=status if refused else None,
                        applied=not refused,
                        input={"changes": it["changes"], "status": status}, actor=self.actor)
        name = mcp_name(it.get("server"), it.get("tool"))
        args = it.get("arguments")
        failed = status == "failed" or bool(it.get("error"))
        return dict(name=name, op=base.OTHER, text=self.T.summary(name, args) if args else name, command=None,
                    writes=None, cwd=None, applied=not failed, input=args, actor=self.actor,
                    error=(str(_d(it.get("error")).get("message") or "failed")[:200]) if failed else None)

    def call(self, n: int, start, done, iid, *, name, op, text, input, actor, writes, command, applied, error, cwd):
        self.info["calls"] += 1
        self.rows.append((n, base.tool_call(self.time(start), self.tool, actor, text, f"{self.rel}:{n}", name=name,
                                            input=input, op=op, session=self.session, id=iid, writes=writes,
                                            command=command, applied=applied, t_result=self.time(done),
                                            error=error, cwd=cwd)))

    def finish(self) -> None:
        """Response items: agent text and calls the items did not already give.
        A code-mode exec is left out when its nested items are here."""
        for n, local, p in self.later:
            try:
                self.response(n, local, p)
            except Exception:                   # one record in a shape nobody foresaw; the rest of the file stands
                self.bad[self.rel] += 1
        self.rows.sort(key=lambda x: x[0])

    def response(self, n: int, local, p: dict) -> None:
        pt = p.get("type")
        if pt == "message":
            if p.get("role") != "assistant":
                return
            text = _text(p.get("content"))
            # the item for the same message: by id, or by text, since many older files give the two
            # different ids, or none
            mid = p.get("id")
            if (isinstance(mid, str) and mid in self.run["ids"]) or self.T.norm(text) in self.agent_texts:
                return
            if text.strip():
                self.rows.append((n, base.agent_text(self.time(local), self.tool, self.actor, text,
                                                     f"{self.rel}:{n}", self.session)))
            return
        cid = p.get("call_id")
        cid = cid if isinstance(cid, str) else None
        if cid is not None and cid in self.run["ids"]:
            return                              # its item is the record, or another file gave it
        if pt == "custom_tool_call" and p.get("name") == "exec" and self.items:
            return
        if cid is not None:
            self.run["ids"].add(cid)            # a fork that copied it gives it again
        name = str(p.get("name") or "?")
        inp = p.get("arguments") if pt == "function_call" else p.get("input")
        if pt == "function_call" and isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except ValueError:
                pass                            # not JSON: kept as the raw text
        if name == "apply_patch" and isinstance(inp, dict):
            inp = inp.get("input") or inp.get("patch") or json.dumps(inp)
        if isinstance(inp, str) and "*** Begin Patch" in inp:
            inp = _patch_text(inp)
        op = _op(name, inp)
        command, cwd = _command(inp) if op == base.COMMAND else (None, None)
        done = self.outputs.get(cid) if cid is not None else None
        self.call(n, local, done, cid, name=name, op=op, text=self.T.summary(name, inp),
                  input=inp, actor=self.actor, writes=patch_writes(inp) if op == base.EDIT else None,
                  command=command, applied=True, error=None, cwd=cwd)


def _under(p, roots: list) -> bool:
    try:
        r = pathlib.Path(p).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    return any(r == x or x in r.parents for x in roots)


def _worktrees(repo: pathlib.Path) -> tuple[list, set]:
    """The repository's linked worktrees, from git's own admin files, and the
    threads Codex recorded as owning them."""
    roots, owners = [], set()
    for d in sorted((repo / ".git" / "worktrees").glob("*")):
        try:
            gitdir = pathlib.Path((d / "gitdir").read_text().strip())
            # worktree.useRelativePaths writes it relative to the admin directory
            roots.append((gitdir if gitdir.is_absolute() else d / gitdir).parent.resolve())
        except (OSError, ValueError):
            pass                            # a half-removed worktree: its rollouts can still match on commit
        try:
            owners.add(str(json.loads((d / "codex-thread.json").read_text())["ownerThreadId"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass                            # not a Codex worktree, or an older one without the record
    return roots, owners


def _window(repo: pathlib.Path) -> tuple[float, float] | None:
    """The session's span on this machine's clock, from recorder.json."""
    import time
    try:
        rec = json.loads((repo / ".interview" / "recorder.json").read_text())
        lo = _num(rec["started"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if lo is None:
        return None
    hi = _num(rec.get("deadline"))
    if hi is not None and rec.get("clock") == "server":
        hi -= _num(rec.get("clock_offset")) or 0.0         # a server-clock deadline, to this machine's
    return lo, hi if hi is not None else time.time()


def _zst_lines(f: pathlib.Path):
    """A compressed rollout's lines, streamed: most are old and need only the
    first. The stdlib reads zstd from 3.14; before that, the zstd command."""
    try:
        from compression import zstd            # type: ignore
    except ImportError:
        zstd = None
    if zstd is not None:
        with zstd.open(f, "rb") as fh:
            yield from fh
        return
    import shutil, subprocess
    exe = shutil.which("zstd")
    if not exe:
        raise OSError("no zstd here")
    p = subprocess.Popen([exe, "-q", "-d", "-c", str(f)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield from p.stdout
        if p.wait(timeout=60) != 0:             # read to the end: its status says whether that was the file
            raise OSError("zstd could not read it")
    finally:
        if p.poll() is None:                    # closed early: only the first line was wanted
            p.kill()
        p.stdout.close()
        p.wait()


def _scan(f: pathlib.Path, deep: bool) -> dict:
    """What ties a rollout to a repository: its cwds (line 1, and with deep
    every turn_context), its thread and rollout ids, lineage, commit, times."""
    packed = f.name.endswith(".zst")
    lines = _zst_lines(f) if packed else f.open("rb")
    try:
        first = next(iter(lines), b"")
        try:
            r = _d(json.loads(first))
        except ValueError:
            r = {}
        meta = _d(r.get("payload")) if r.get("type") == "session_meta" else {}
        cwds = [meta["cwd"]] if isinstance(meta.get("cwd"), str) else []
        if deep:
            for raw in lines:
                if b'"turn_context"' in raw[:200]:
                    try:
                        cwd = _d(_d(json.loads(raw)).get("payload")).get("cwd")
                    except ValueError:
                        continue                # a torn line
                    if isinstance(cwd, str) and cwd not in cwds:
                        cwds.append(cwd)
    finally:
        lines.close()
    m = NAME.search(f.name)
    thread = str(meta.get("id") or (m.group(1) if m else f.name))
    src = _d(_d(meta.get("source")).get("subagent"))
    links = {x for x in (meta.get("forked_from_id"), meta.get("parent_thread_id"),
                         _d(meta.get("history_base")).get("thread_id"),
                         _d(src.get("thread_spawn")).get("parent_thread_id")) if isinstance(x, str)}
    commit = _d(meta.get("git")).get("commit_hash")
    return {"thread": thread, "rollout": (m.group(2) or m.group(1)) if m else thread, "cwds": cwds, "links": links,
            "commit": commit if isinstance(commit, str) else None, "t0": _epoch(meta.get("timestamp")),
            "mtime": f.stat().st_mtime, "packed": packed, "deep": deep, "inherits": _inherited(meta) is not None}


def _trim(rows: list, roots: list) -> list | None:
    """A kept rollout as it may leave: None to take it whole, else its
    session_meta line and the thread's own records from its first turn here,
    [] when none of its own turns ran here. A thread started in another
    project and moved here (/cd, a handoff) leaves its earlier turns behind,
    and a later compaction's summary of them. A fork or subagent never brings
    the records it took from its parent: the parser skips them, and they may
    be from before the move."""
    def load(raw) -> dict:
        try:
            r = json.loads(raw)
        except (ValueError, RecursionError):
            return {}                           # a torn line: it goes with its neighbours
        return r if isinstance(r, dict) else {}
    recs = [load(x) for x in rows]
    first = 1 if recs and recs[0].get("type") == "session_meta" else 0
    mark = _inherited(_d(recs[0].get("payload"))) if first else None
    copied = lambda r: mark is not None and isinstance(r.get("ordinal"), int) and \
        not isinstance(r.get("ordinal"), bool) and r["ordinal"] < mark
    here = lambda r: _under(_d(r.get("payload")).get("cwd"), roots)
    kind = lambda r: (r.get("type"), _d(r.get("payload")).get("type"))
    own = [i for i in range(first, len(recs)) if not copied(recs[i])]
    turns = [i for i in own if recs[i].get("type") == "turn_context"]
    start = own[0] if own else len(recs)
    if turns and not here(recs[turns[0]]):
        k = next((i for i in turns if here(recs[i])), None)
        if k is None:
            return []
        start = k
        for j in range(k - 1, first - 1, -1):   # back to the event that began that turn
            if copied(recs[j]) or kind(recs[j])[0] == "turn_context" or \
                    kind(recs[j]) in (("event_msg", "task_complete"), ("event_msg", "turn_complete")):
                break
            if kind(recs[j]) in (("event_msg", "task_started"), ("event_msg", "turn_started")):
                start = j
                break
    cut = bool(own) and start > own[0]
    if not cut and len(own) == len(recs) - first:
        return None
    foreign = cut or any(copied(r) and r.get("type") == "turn_context" and not here(r) for r in recs[first:])
    out = rows[:first]
    for i in own:
        if i >= start:
            out.append((json.dumps({**recs[i], "payload": {}}) + "\n").encode()
                       if foreign and recs[i].get("type") == "compacted" else rows[i])
    return out


def _has_commit(repo: pathlib.Path, sha, cache: dict) -> bool:
    """The worktree is gone but its objects are the repository's: a commit made
    there, or the one it started from, is still in the repository."""
    import subprocess
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        return False
    if sha not in cache:
        try:
            cache[sha] = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                                        capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            cache[sha] = False
    return cache[sha]


def _codex_worktree(cwd: str, root: pathlib.Path, repo_name: str) -> bool:
    """Codex's own layout under its worktree root: <hex4>/<repo name>/..., or
    the older flat <name>/..., whose name says nothing about the repository."""
    try:
        parts = pathlib.Path(cwd).resolve().relative_to(root).parts
    except (OSError, RuntimeError, ValueError, TypeError):
        return False
    if not parts:
        return False
    return parts[1:2] == (repo_name,) if re.fullmatch(r"[0-9a-f]{4}", parts[0]) else True


class Codex(base.Adapter):
    name = "codex"
    title = "Codex"

    def detect(self) -> str | None:
        """The CLI a terminal runs (else the one the desktop app bundles), and
        the desktop app."""
        import plistlib, shutil, subprocess
        app = None
        for a in APPS:
            try:
                info = plistlib.loads((a / "Contents" / "Info.plist").read_bytes())
            except (OSError, ValueError):
                continue
            if isinstance(info, dict) and info.get("CFBundleIdentifier") == BUNDLE_ID:
                app = (a, str(info.get("CFBundleShortVersionString") or "installed"))
                break
        cli = shutil.which("codex") or (str(app[0] / BUNDLED_CLI) if app and (app[0] / BUNDLED_CLI).is_file()
                                        else None)
        version = None
        if cli:
            try:
                r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=3)
                m = re.search(r"\b(\d+\.\d+\.\d+\S*)", r.stdout or "")
                version = m.group(1) if m else "installed"
            except (OSError, subprocess.TimeoutExpired):
                version = "installed"
        bits = ([f"CLI {version}"] if version else []) + ([f"desktop app {app[1]}"] if app else [])
        return ", ".join(bits) or None

    def collect(self, repo: pathlib.Path, out: pathlib.Path, save) -> tuple[list[str], list[str]]:
        """This repository's threads, and only those. A thread is kept when a
        cwd it ran in is the repository or one of its worktrees, when it owns
        one of those worktrees, or when it descends from a kept thread (a fork,
        a subagent, a revert's new file). A thread whose worktree was removed
        is kept as probable when its path has Codex's worktree layout, its
        commit is in the repository, and it ran during the session. Of a
        thread started elsewhere, only its turns from the first one here
        leave (_trim)."""
        import tempfile
        from .. import paths as P
        home = P.codex_home()
        repo = pathlib.Path(repo).resolve()
        trees, owners = _worktrees(repo)
        roots = [repo] + trees
        window = _window(repo)
        files = []
        for sub in ("sessions", "archived_sessions"):
            for pattern in ("**/*.jsonl", "**/*.jsonl.zst"):
                files += [(sub, f) for f in sorted((home / sub).glob(pattern)) if f.is_file()]
        scans, unread = {}, 0
        for _sub, f in files:
            try:
                # a file untouched since the session began holds none of it: read its first line only
                deep = window is None or f.stat().st_mtime >= window[0] - WINDOW_SLACK
                scans[f] = _scan(f, deep)
            except Exception:                   # unreadable, or a shape nobody foresaw: that file alone
                unread += 1
        keep: dict = {}
        for f, s in scans.items():
            if any(_under(c, roots) for c in s["cwds"]):
                keep[f] = "tree" if not any(_under(c, [repo]) for c in s["cwds"]) else "repo"
            elif s["thread"] in owners:
                keep[f] = "owner"
        self._descendants(scans, keep)
        wt_root = (home / "worktrees").resolve()
        cache: dict = {}
        for f, s in scans.items():
            if f in keep or window is None or s["t0"] is None:
                continue
            if s["t0"] <= window[1] + WINDOW_SLACK and max(s["mtime"], s["t0"]) >= window[0] - WINDOW_SLACK \
                    and any(_codex_worktree(c, wt_root, repo.name) for c in s["cwds"]) \
                    and _has_commit(repo, s["commit"], cache):
                keep[f] = "probable"
        self._descendants(scans, keep)

        names, probable = set(), {}
        counts = {"all": 0, "archived": 0, "tree": 0, "probable": 0}
        for sub, f in files:
            if f not in keep:
                continue
            s = scans[f]
            name = f.name[:-4] if s["packed"] else f.name
            if name in names:
                continue
            try:
                # a thread matched on its cwd or lineage may hold turns elsewhere; its whole scan
                # all here, with nothing taken from a parent, says it does not
                rows = cut = None
                if keep[f] in ("repo", "tree", "lineage") and \
                        not (s["deep"] and not s["inherits"] and all(_under(c, roots) for c in s["cwds"])):
                    if s["packed"]:
                        rows = list(_zst_lines(f))
                    else:
                        with f.open("rb") as fh:
                            rows = fh.readlines()
                    cut = _trim(rows, roots)
                    if cut == []:
                        continue                        # none of its turns ran here
                if s["packed"] or cut is not None:
                    with tempfile.TemporaryDirectory() as t:
                        with open(pathlib.Path(t) / name, "wb") as fh:
                            fh.writelines(cut if cut is not None else rows if rows is not None else _zst_lines(f))
                        ok = save("", name, pathlib.Path(t) / name)
                else:
                    ok = save("", name, f)
            except OSError:
                unread += 1
                continue
            if not ok:
                continue
            names.add(name)
            counts["all"] += 1
            counts["archived"] += sub == "archived_sessions"
            counts["tree"] += keep[f] == "tree"
            if keep[f].startswith("probable"):
                counts["probable"] += 1
                probable[name] = ("descends from a probable match" if keep[f] == "probable-lineage" else
                                  f"cwd {s['cwds'][0] if s['cwds'] else '?'} is a Codex worktree that is gone; "
                                  f"commit {str(s['commit'])[:12]} is in the repository")
        if probable or (pathlib.Path(out) / "collect.json").exists():
            save("", "collect.json", {"probable": probable})        # an earlier run's note would mislead
        found, skipped = [], []
        if counts["all"]:
            bits = [f"Codex: {counts['all']} session(s)"]
            if counts["archived"]:
                bits.append(f"{counts['archived']} of them archived")
            if counts["tree"]:
                bits.append(f"{counts['tree']} in a worktree")
            if counts["probable"]:
                bits.append(f"{counts['probable']} probable (its worktree was removed; matched on commit and time)")
            found.append(", ".join(bits))
        if unread:
            skipped.append(f"Codex: {unread} rollout(s) could not be read here, so could not be matched "
                           "(compressed with no zstd, or unreadable)")
        return found, skipped

    @staticmethod
    def _descendants(scans: dict, keep: dict) -> None:
        """Forks, subagents, a revert's new file: anything that names a kept
        thread or rollout, or is one of a kept thread's files, to a fixpoint.
        What descends from a probable match is probable too."""
        grew = True
        while grew:
            grew = False
            ids = {}
            for f, why in keep.items():
                for k in (scans[f]["thread"], scans[f]["rollout"]):
                    ids[k] = "probable" if why.startswith("probable") or ids.get(k) == "probable" else why
            for f, s in scans.items():
                if f in keep:
                    continue
                hit = [ids[k] for k in [s["thread"], *sorted(s["links"])] if k in ids]
                if hit:
                    keep[f] = "probable-lineage" if "probable" in hit else "lineage"
                    grew = True

    def hook_files(self, python: str, hook_script: str, repo_root: str | None = None) -> dict[str, str]:
        """Codex trusts each hook by a hash of its definition, so this must give
        the same bytes for the same arguments: any change asks the candidate to
        trust the hooks again. repo_root is not used: hooks run in the session's
        cwd, and a linked worktree loads them from the root checkout.

        Exit 2 blocks a call or a prompt, and on Stop makes stderr the agent's
        next instruction; python exits 2 when the script is gone (the folder
        moved since setup), so the command never fails."""
        def h(event: str, timeout: int = 5) -> dict:
            return {"hooks": [{"type": "command",
                               "command": f'"{python}" "{hook_script}" --tool codex {event} || true',
                               "timeout": timeout}]}
        hooks = {
            "SessionStart": [h("session_start")],
            "UserPromptSubmit": [h("user_prompt")],
            "PreToolUse": [{"matcher": "*", **h("tool_use")}],
            "PostToolUse": [{"matcher": "*", **h("tool_result")}],
            "Stop": [h("stop")],
            "SessionEnd": [h("session_end", 3)],        # Codex caps SessionEnd at 3 s and warns above it
        }
        return {".codex/hooks.json": json.dumps({"hooks": hooks}, indent=2) + "\n"}

    def mcp_files(self, python: str, mcp_script: str, repo_root: str | None = None) -> dict[str, str]:
        """A stdio server with no cwd starts in the session's cwd, a
        subdirectory when Codex was opened in one; Codex has no project-root
        variable, so the build-time default stays relative and setup makes it
        exact. TOML literal strings, so Windows backslashes need no escaping;
        a path a literal string cannot hold gets an escaped basic string.

        The panel's tools (ask_panel, time_left) only answer; Codex would ask
        before each call, and `codex exec` refuses a call that needs asking."""
        q = lambda v: f"'{v}'" if "'" not in v and "\n" not in v else json.dumps(v)
        s = configs.SERVER
        return {".codex/config.toml":
                f"# Panel for this session. `interview setup` rewrites the paths for this machine.\n"
                f"[mcp_servers.{s}]\n"
                f"command = {q(python)}\n"
                f"args = [{q(mcp_script)}]\n"
                + (f"cwd = {q(repo_root)}\n" if repo_root else "") +
                f"startup_timeout_sec = 20\n"
                f"default_tools_approval_mode = \"approve\"\n\n"
                f"[mcp_servers.{s}.env]\n"
                f"INTERVIEW_REPO = {q(repo_root or '.')}\n"}

    def from_hook(self, rec: dict) -> dict | None:
        """Codex's hooks name a shell call Bash and an edit apply_patch; both
        carry their text in tool_input.command, the script or the patch."""
        name, inp = str(rec.get("tool_name") or "?"), rec.get("tool_input")
        text = inp.get("command") if isinstance(inp, dict) else inp
        text = str(text) if text is not None else ""
        if name == "Bash":
            return {"name": name, "input": inp, "op": base.COMMAND, "writes": None, "command": text}
        if name == "apply_patch":
            return {"name": name, "input": inp, "op": base.EDIT, "writes": patch_writes(text), "command": None}
        return {"name": name, "input": inp, "op": _op(name, inp), "writes": None, "command": None}

    def parse(self, tr: dict, logs: pathlib.Path, bad) -> dict:
        from .. import trace
        out = base.parsed()
        files = sorted(p for p in logs.rglob("*.jsonl") if p.is_file())
        metas = {f: _meta(f) for f in files}
        rel = {f: f"{self.name}/{f.relative_to(logs)}" for f in files}
        by_rollout = {rollout_id(f, metas[f]): rel[f] for f in files}

        def created(f):
            try:
                return trace.epoch(metas[f].get("_t")) or float("inf")
            except (TypeError, ValueError, OverflowError):
                return float("inf")
        # parents before children: a fork, revert or subagent starts after its parent
        order = sorted(files, key=lambda f: (created(f), f.name))
        run = {"ids": set(), "prompts": []}
        unknown, versions = [], set()
        try:
            probable = _d(json.loads((logs / "collect.json").read_text()).get("probable"))
        except (OSError, ValueError, AttributeError):
            probable = {}                        # nothing collected on a guess
        for f in order:
            try:
                one = self._file(tr, f, rel[f], metas[f], run, bad, by_rollout, probable)
            except Exception as e:               # a file in a shape nobody foresaw costs that file, said so
                tr["caveats"].append(f"could not read {rel[f]}: {type(e).__name__}: {str(e)[:200]}")
                continue
            meta = metas[f]
            mode = meta.get("history_mode")
            if mode not in MODES and not one.readable:
                why = "no session_meta" if meta.get("_none") else \
                    "no history_mode" if mode is None else f"history_mode {str(mode)[:40]!r}"
                unknown.append((why, one.info["first"], one.info["last"]))
            if isinstance(meta.get("cli_version"), str):
                versions.add(meta["cli_version"])
            out["subagents" if one.sub else "sessions"].append(one.info)
            for _, e in one.rows:
                out["prompts" if "kind" not in e else "events"].append(e)
        packed = sorted(p for p in logs.rglob("*.jsonl.zst") if p.is_file())
        unknown += [("compressed (.zst), not readable here", None, None)] * len(packed)
        cov = out["coverage"]
        cov["version"] = ", ".join(sorted(versions)) or None
        if unknown:
            times = [t for _, a, b in unknown for t in (a, b) if t is not None]
            cov.update(format=f"unrecognised: {len(unknown)} of {len(files) + len(packed)} rollout(s) with "
                              + ", ".join(sorted({w for w, _, _ in unknown})) + " and no record this adapter reads",
                       first=min(times) if times else None, last=max(times) if times else None)
        return out

    def _file(self, tr, f, rel, meta, run, bad, by_rollout, probable) -> _File:
        from .. import trace
        one = _File(self, tr, f, rel, meta, run, bad)
        for n, r in trace.rows(f, bad, rel):
            try:
                if n == 1 and r.get("type") == "session_meta":
                    one.seen(one.time(trace.epoch(r.get("timestamp"))))
                else:
                    one.line(n, r)
            except Exception:                   # one record in a shape nobody foresaw; the rest of the file stands
                bad[rel] += 1
        one.finish()
        hb = _d(meta.get("history_base"))
        if hb:
            parent = by_rollout.get(str(hb.get("thread_id")))
            one.info["history_base"] = {"file": parent, "rollout": hb.get("thread_id"),
                                        "end_ordinal": hb.get("end_ordinal_exclusive")}
            if parent is None:
                tr["caveats"].append(f"{rel} inherits its first {hb.get('end_ordinal_exclusive')} record(s) "
                                     f"from rollout {hb.get('thread_id')}, which was not collected: that part "
                                     "of the conversation is missing")
        if f.name in probable:
            one.info["probable"] = str(probable[f.name])
            tr["caveats"].append(f"{rel} was collected as a probable match, not a certain one: {probable[f.name]}")
        one.info["models"] = sorted(one.info["models"])
        return one


ADAPTER = Codex()
