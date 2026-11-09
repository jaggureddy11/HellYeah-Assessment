#!/usr/bin/env python3
"""Gathers the chat history your coding tool kept for THIS project.

    ./interview collect

`interview submit` runs it for you at the end, and every checkpoint takes its
own copy the same way; run it yourself to see what it would take. It writes
into .interview/agent_logs/, which goes into your submission with the rest of
.interview/, and prints what it found.

It is scoped on purpose. It only takes records whose working directory is this
repository, so your other projects are not touched. Where a tool cannot be
filtered safely, it tells you and leaves it alone.

Nothing here is hidden. Read the file, and for the tools with a module of
their own, that module's collect() in agents/.
"""
from __future__ import annotations
import json, shutil, sqlite3, pathlib

from . import agents, paths as P

ROOT = pathlib.Path(".").resolve()
OUT = ROOT / ".interview" / "agent_logs"
HOME = P.HOME


def configure(repo: pathlib.Path, out: pathlib.Path | None = None):
    """`out` elsewhere than .interview/agent_logs is for checkpoints, which
    collect into a temporary directory and leave the repository alone."""
    global ROOT, OUT
    ROOT = pathlib.Path(repo).resolve()
    OUT = pathlib.Path(out) if out is not None else ROOT / ".interview" / "agent_logs"
found: list[str] = []
skipped: list[str] = []

BUDGET = 400 * 1024 * 1024        # stop before an accident becomes a huge upload
_taken = {"bytes": 0, "over": False}
# copied file -> its source's mtime. For the tools with no adapter these go
# into <tool>/collect.json: the copies in a submission lose their own, and
# for most of those tools a file's last write is the only time there is.
_mtimes: dict = {}
LEGACY = ("aider", "gemini", "windsurf")


def save(sub: str, name: str, data) -> pathlib.Path | None:
    """Copies one file, or writes one JSON blob. Directories and anything past
    the size budget are skipped rather than aborting the run."""
    if _taken["over"]:
        return None
    d = OUT / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if isinstance(data, (dict, list)):
        blob = json.dumps(data, ensure_ascii=False, indent=1)
        _taken["bytes"] += len(blob)
        p.write_text(blob)
        return p
    src = pathlib.Path(data)
    if not src.is_file():
        return None
    n = src.stat().st_size
    if _taken["bytes"] + n > BUDGET:
        _taken["over"] = True
        skipped.append(f"size budget of {BUDGET // 1024 // 1024} MB reached; "
                       f"stopped collecting. Tell the panel.")
        return None
    _taken["bytes"] += n
    shutil.copy2(src, p)
    _mtimes[p] = src.stat().st_mtime
    return p


def aider():
    n = 0
    for name in (".aider.chat.history.md", ".aider.input.history"):
        p = ROOT / name
        if p.exists():
            save("aider", name, p)
            n += 1
    if n:
        found.append(f"Aider: {n} file(s)")


def gemini():
    """Gemini CLI keeps per-project state under tmp/<sha256 of the project
    root>/ (chats/, logs.json). Matching on that hash is structural; a chat that
    merely mentions this path is not ours. Rule taken from gemini-cli's
    getProjectHash; not verified against a local install."""
    import hashlib
    base = P.cli_history_roots()["Gemini CLI"]
    if not base.exists():
        return
    d = base / "tmp" / hashlib.sha256(str(ROOT).encode()).hexdigest()
    if not d.is_dir():
        skipped.append("Gemini CLI: installed, but no project store for this repository.")
        return
    n = 0
    for f in d.rglob("*.json"):
        if f.is_file() and save(f"gemini/{f.parent.relative_to(d)}", f.name, f):
            n += 1
    if n:
        found.append(f"Gemini CLI: {n} file(s)")


_TMP: list[pathlib.Path] = []      # copies of editor stores, removed at the end of run()


def _open_sqlite(db: pathlib.Path) -> sqlite3.Connection:
    """The editor holds its store open and locked. Copy it, with its write-ahead
    log, and read the copy; fall back to immutable mode if the copy fails."""
    import tempfile
    try:
        t = pathlib.Path(tempfile.mkdtemp(prefix="ivkit-db-"))
        _TMP.append(t)
        shutil.copy2(db, t / db.name)
        for side in ("-wal", "-shm"):
            if (db.parent / (db.name + side)).exists():
                shutil.copy2(db.parent / (db.name + side), t / (db.name + side))
        con = sqlite3.connect(t / db.name)
        con.execute("select 1")
        return con
    except Exception:
        return sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)


def _item(con: sqlite3.Connection, key: str):
    r = con.execute("select value from ItemTable where key=?", (key,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except Exception:
        return r[0] if isinstance(r[0], str) else str(r[0])


def _workspace_matches(wd: pathlib.Path) -> bool:
    wj = wd / "workspace.json"
    if not wj.exists():
        return False
    try:
        target = json.loads(wj.read_text()).get("folder", "")
    except Exception:
        return False
    if not target or ROOT.name not in target:
        return False
    from urllib.parse import unquote
    return pathlib.Path(unquote(target.replace("file://", ""))).resolve() == ROOT


def windsurf():
    """Windsurf keeps per-workspace state in a SQLite file, as the VS Code it
    is built on does. Only the workspace whose folder is this repository is
    read. Cursor and Copilot in VS Code have adapters of their own."""
    want_keys = ("aiService.prompts", "aiService.generations", "chat.ChatSessionStore.index",
                 "interactive.sessions")
    for base in P.editor_workspace_roots().get("Windsurf", []):
        if not base.exists():
            continue
        matched = False
        for wd in base.glob("*"):
            if not _workspace_matches(wd):
                continue
            matched = True
            bits = []
            sessions = 0
            for f in (wd / "chatSessions").glob("*"):
                if f.is_file() and save(f"windsurf/{wd.name}/chatSessions", f.name, f):
                    sessions += 1
            if sessions:
                bits.append(f"{sessions} chat session file(s)")
            db = wd / "state.vscdb"
            if db.exists():
                try:
                    con = _open_sqlite(db)
                    bundle = {k: v for k in want_keys if (v := _item(con, k)) is not None}
                    con.close()
                    if bundle:
                        p = save(f"windsurf/{wd.name}", "state.json", bundle)
                        if p:           # the database's last write, its log's included
                            _mtimes[p] = max(f.stat().st_mtime for f in (db, wd / "state.vscdb-wal") if f.exists())
                        bits.append("workspace state")
                except Exception as e:
                    skipped.append(f"Windsurf: could not read {db.name} ({type(e).__name__}: {e})")
            if bits:
                found.append("Windsurf: " + ", ".join(bits))
        if not matched:
            skipped.append("Windsurf: installed, but no workspace here. If you used it, export the chat yourself.")


def _saver(name: str):
    """save() for one adapter, `sub` relative to its own agent_logs/<name>/.
    A `sub` that already starts with the name is taken as relative to
    agent_logs/, as save() itself reads it, so either reading of the contract
    lands in the same place."""
    def put(sub: str, fname: str, data):
        sub = str(sub or "").strip("/")
        if sub != name and not sub.startswith(name + "/"):
            sub = f"{name}/{sub}" if sub else name
        return save(sub, fname, data)
    return put


def run(repo=None, out=None, quiet=False):
    if repo is not None:
        configure(repo, out)
    found.clear(); skipped.clear(); _mtimes.clear()
    _taken.update({"bytes": 0, "over": False})
    for a in agents.adapters():
        try:
            f, s = a.collect(ROOT, OUT / a.name, _saver(a.name))
            found.extend(f)
            skipped.extend(s)
        except Exception as e:
            skipped.append(f"{a.title}: {type(e).__name__}")
    # the tools with no adapter
    for fn in (aider, gemini, windsurf):
        try:
            fn()
        except Exception as e:
            skipped.append(f"{fn.__name__}: {type(e).__name__}")
    for t in _TMP:
        shutil.rmtree(t, ignore_errors=True)
    _TMP.clear()
    own = {a.name for a in agents.adapters()}
    for top in LEGACY:
        d = OUT / top
        got = {str(p.relative_to(d)): t for p, t in _mtimes.items() if d in p.parents}
        if got and top not in own:
            (d / "collect.json").write_text(json.dumps({"mtimes": got}, indent=1))

    mb = _taken["bytes"] / 1e6
    # Where it looked, and whether it stopped early: two runs that differ here
    # can legitimately find different transcripts. `skipped` names a tool whose
    # store was read in part, so verify does not flag the calls that are missing.
    collection = {"claude_home": str(P.claude_home()), "codex_home": str(P.codex_home()),
                  "over_budget": _taken["over"], "skipped": list(skipped)}
    if quiet:
        return {"found": list(found), "skipped": list(skipped), "bytes": _taken["bytes"],
                "collection": collection}
    print()
    print(f"  {mb:.1f} MB gathered")
    for f in found:
        print(f"  collected  {f}")
    for s in skipped:
        print(f"  --         {s}")
    if not found:
        print("  nothing found automatically.")
    print()
    if found and ROOT in OUT.parents:
        print(f"Written to {OUT.relative_to(ROOT)}/. It goes into your submission with the rest of .interview/; "
              "there is nothing to commit.")
    elif found:                             # a preview beside a running recorder
        print("`interview submit` collects again when it packages your submission.")
    print("Working-tree snapshots in .interview/snapshots.git are separate from all this.")
    return {"found": list(found), "skipped": list(skipped), "bytes": _taken["bytes"],
            "collection": collection}


