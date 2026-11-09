"""Sealing a problem pack into a kit, and opening it at minute 0.

The kit can be handed over days early: the exercise inside is encrypted, and the
passphrase is read out at the start. Delivery of the data and delivery of the
secret travel on different channels, so the candidate can fix environment
problems ahead of time without seeing the problem.

Cross-platform: tarring and untarring use the standard library. Encryption uses
openssl, which ships with macOS, Linux and Git for Windows.
"""
from __future__ import annotations
import datetime, hashlib, json, os, pathlib, secrets, shutil, subprocess, tarfile, tempfile

ITER = 200_000
# PBKDF2 rounds for the gate's passphrase check. Kept low on purpose: the record
# that holds this hash also holds the payload key, so the hash guards nothing a
# reader of the record lacks, and the Worker computes it on every start.
GATE_ITER = 10_000

# Never shipped to a candidate. They hold no secrets, but a candidate's machine
# has no business carrying the tooling that mints upload links or grades the candidate.
PANEL_ONLY_MODULES = ("creds.py", "presign.py", "links.py", "verify.py", "evaluate.py",
                      "review.py", "render.py", "trace.py", "provenance.py", "digest.py",
                      "gitsafe.py", "crosscheck.py", "reproduce.py", "notify.py")
CIPHER = ["-aes-256-cbc", "-pbkdf2", "-iter", str(ITER), "-salt"]


class SealError(RuntimeError):
    pass


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat(timespec="seconds")


def _openssl() -> str:
    exe = shutil.which("openssl")
    if not exe:
        raise SealError("openssl not found. On Windows it ships with Git for Windows.")
    return exe


def _world_data(pack, seed: int, dst: pathlib.Path) -> None:
    """Replaces each file of the staged data/ with world `seed`'s file of the
    same name. Only those names: the world is kept beside its ground truth,
    which is not the candidate's. And only the world folder's own files, judged
    on disk: copy2 follows a link, to the ground truth as readily as to data."""
    src = pack.world_data(seed)
    if src is None or not src.is_dir():
        raise SealError(f"world {seed} of {pack.name} has no data at {src}")
    if not _under(src, pathlib.Path(pack.root)):
        raise SealError(f"world {seed}'s data {src} resolves outside the pack ({src.resolve()}). "
                        f"Sealing ships only the pack's own files.")
    for rel in sorted(p.relative_to(dst) for p in dst.rglob("*") if p.is_file()):
        if not (src / rel).is_file():
            raise SealError(f"world {seed} has no {rel}: its data would not be the shape the brief describes")
        if (src / rel).resolve() != src.resolve() / rel:
            raise SealError(f"world {seed}'s {rel} is a link to {(src / rel).resolve()}. Sealing copies the "
                            f"world's own files, never what a link in it points at.")
        shutil.copy2(src / rel, dst / rel)


def _under(p: pathlib.Path, n: pathlib.Path) -> bool:
    """p is n or inside it, as the disk has them: `..` and symlinks resolved,
    and on a disk that ignores case, the same folder under another case."""
    p, n = p.resolve(), n.resolve()
    if p == n or n in p.parents:
        return True
    return n.exists() and any(q.exists() and os.path.samefile(q, n) for q in (p, *p.parents))


def _refuse_internal(pack, never: list) -> None:
    """What ships (the sealed paths, everything inside a sealed folder, each
    tool's module) is inside the pack and under no never_ship entry, judged
    on disk: a literal path could get there through `..` or a symlink."""
    root = pathlib.Path(pack.root)

    def check(rel: str, sits_under):
        top = root / rel
        for p in [top] + (sorted(top.rglob("*")) if top.is_dir() and not top.is_symlink() else []):
            if not _under(p, root):
                raise SealError(f"'{rel}' resolves outside the pack ({p.resolve()}). Sealing ships only the pack's own files.")
            for n in never:
                if _under(p, root / n):
                    raise SealError(sits_under(n))

    for rel in pack.sealed:
        check(rel, lambda n, rel=rel: f"'{rel}' is listed in sealed but sits under never_ship "
                                      f"'{n}'. Sealing would hand the candidate your internal material.")
    for t in pack.tools:
        if (root / t["module"]).exists():
            check(t["module"], lambda n, t=t: f"tool '{t.get('name')}' has its module '{t['module']}' under never_ship "
                                              f"'{n}'. Sealing would hand the candidate your internal material.")


def gate_record(doc: dict, passphrase: str, minutes: float, key: str,
                now: datetime.datetime | None = None, pausable: bool = False) -> dict:
    """What the gate (gate/worker.js) reads from gate/<id>.json: how to check
    the passphrase, the key to hand over, and when the session may open. A live
    session may open while its upload links still outlive the deadline by the
    grace; a take-home not before its start, nor after its deadline.

    A pausable session's minutes are a budget of active time that may be spent
    until the window's end, the links' expiry less the grace (spec §3). It may
    still open only while one unbroken sitting fits."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    prefix = doc["prefix"]
    slug, session = prefix.split("/")[-2:]
    expires = datetime.datetime.fromisoformat(doc["expires_at"]).timestamp()
    grace = float(doc.get("grace_minutes", 15))
    sched = doc.get("schedule") or {}
    if pausable and sched.get("deadline"):
        raise SealError("a take-home with a fixed start and deadline keeps one unbroken stretch; "
                        "it cannot also be pausable")
    epoch = lambda s: datetime.datetime.fromisoformat(s).timestamp()
    if sched.get("deadline"):
        not_before, deadline = epoch(sched["start"]), epoch(sched["deadline"])
        open_until = deadline
    else:
        not_before = deadline = None
        open_until = expires - (minutes + grace) * 60
    if open_until <= now.timestamp():
        raise SealError(f"this take-home's deadline {sched['deadline']} has passed" if sched.get("deadline") else
                        f"the upload links expire {doc['expires_at']}, too soon for a {minutes:g}-minute "
                        f"session plus {grace:g} minutes' grace: nobody could start it. Presign for longer.")
    salt = secrets.token_hex(16)
    rec = {"id": f"{slug}-{session}", "prefix": prefix, "salt": salt, "kdf": "pbkdf2-sha256",
           "iterations": GATE_ITER, "verifier": gate_verifier(passphrase, salt, GATE_ITER),
           "key": key, "minutes": minutes, "open_until": open_until,
           "not_before": not_before, "deadline": deadline}
    if pausable:
        rec.update(pausable=True, window_end=expires - grace * 60)
    return rec


def gate_verifier(passphrase: str, salt: str, iterations: int = GATE_ITER) -> str:
    """What gate/worker.js recomputes from the passphrase the kit sends."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.strip().encode(), salt.encode(), iterations).hex()


def build(pack, out: pathlib.Path, passphrase: str,
          upload: pathlib.Path | None = None, minutes: float | None = None,
          world: int | None = None, gate: str | None = None,
          gate_out: pathlib.Path | None = None, pausable: bool = False) -> pathlib.Path:
    """Produces the repository handed to the candidate. `minutes` replaces the
    pack's clock, for rehearsals; it is sealed in like everything else. `world`
    is one of the pack's world_seeds: that world's data is sealed in place of
    data/, which is the first world's.

    `gate` is the clock gate's URL. With it the payload is sealed with a random
    key rather than the passphrase, and the record the gate needs (that key, and
    a check for the passphrase) is written to `gate_out` for the panel to put in
    the bucket: the kit alone cannot open, and the clock starts on the server
    when the passphrase is first used. Without it the passphrase is the key.
    `pausable` (with `gate` only) makes the minutes a budget the candidate may
    pause (spec §2)."""
    worlds = getattr(pack, "world_seeds", []) or []
    if world is not None and int(world) not in worlds:
        raise SealError(f"world {world} is not one of {pack.name}'s validated world_seeds "
                        f"({', '.join(map(str, worlds)) or 'none'})")
    if world is not None and "data" not in pack.sealed:
        raise SealError(f"{pack.name} seals no data/ for world {world} to stand in for")
    doc = json.loads(upload.read_text()) if upload and upload.exists() else {}
    if pausable and not gate:
        raise SealError("a pausable kit needs the clock gate (--gate): the gate keeps its clock")
    record = None
    if gate:
        if not (doc.get("prefix") and gate_out):
            raise SealError("a gated kit needs its upload links (--upload) and somewhere to write "
                            "the gate's record (--gate-record)")
        if _under(pathlib.Path(gate_out).parent, pathlib.Path(out)):
            raise SealError(f"the gate's record holds the payload's key; {gate_out} is inside the kit")
        record = gate_record(doc, passphrase, float(minutes or pack.build_minutes), secrets.token_hex(32),
                             pausable=pausable)
    # before `out` is touched: a refused build leaves the previous kit where it was
    _refuse_internal(pack, list(getattr(pack, "never_ship", []) or []))
    out = pathlib.Path(out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        stage = pathlib.Path(tmp) / "sealed"
        stage.mkdir()
        for rel in pack.sealed:
            src = pack.root / rel
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
        if world is not None and int(world) != worlds[0]:
            _world_data(pack, int(world), stage / "data")
        # the world is not written anywhere in the kit: problem.json is the candidate's to read
        meta = {"name": pack.name, "title": pack.title,
                "build_minutes": minutes or pack.build_minutes,
                "review_minutes": pack.review_minutes,
                "brief": pack.meta["brief"],
                "tools": pack.tools}
        for t in pack.tools:                      # ship each tool's module too
            src = pack.root / t["module"]
            if src.exists():
                dst = stage / t["module"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        (stage / "problem.json").write_text(json.dumps(meta, indent=1))
        if upload and upload.exists():
            iv = stage / ".interview"
            iv.mkdir(parents=True, exist_ok=True)
            (iv / "upload.json").write_text(upload.read_text())

        tar_path = pathlib.Path(tmp) / "payload.tar"
        with tarfile.open(tar_path, "w") as tf:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    tf.add(p, arcname=str(p.relative_to(stage)))
        subprocess.run([_openssl(), "enc", *CIPHER, "-in", str(tar_path),
                        "-out", str(out / "payload.enc"), "-pass", "stdin"],
                       input=(record["key"] if record else passphrase) + "\n", text=True,
                       check=True, capture_output=True)

    # The tooling lives inside .interview so that the candidate's repository root
    # stays the candidate's own. We are judging that root; we should not be cluttering it.
    here = pathlib.Path(__file__).resolve().parent
    shutil.copytree(here, out / ".interview" / "ivkit",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  *PANEL_ONLY_MODULES))
    (out / ".interview" / "ivkit" / "_role").write_text("candidate\n")
    from . import launcher
    (out / ".interview" / "ivkit" / "_entry.py").write_text(launcher.ENTRY)
    (out / "interview").write_text(launcher.POSIX)
    (out / "interview.cmd").write_text(launcher.WINDOWS)
    os.chmod(out / "interview", 0o755)
    (out / ".gitignore").write_text("\n".join(GITIGNORE) + "\n")
    _write_mcp_config(out)
    # Not secret, and readable before the passphrase: `start` refuses a closed
    # session without unsealing it, and finds the gate here.
    session = {}
    if doc.get("schedule"):
        session.update(start=doc["schedule"]["start"], deadline=doc["schedule"]["deadline"])
    if record:
        session.update(gate=gate, id=record["id"], open_until=_iso(record["open_until"]))
        if record.get("pausable"):
            session.update(pausable=True, window_end=_iso(record["window_end"]))
    if session:
        (out / ".interview" / "session.json").write_text(json.dumps(session, indent=1))
    every = (doc.get("checkpoints") or [{}])[0].get("offset_minutes")
    (out / "README.md").write_text(_readme(pack, minutes or pack.build_minutes, doc.get("schedule"), every,
                                           session.get("open_until") if record else None,
                                           pausable=bool(record and record.get("pausable")),
                                           window_end=session.get("window_end")))

    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=out,
                   capture_output=True)
    write_exclude(out)
    subprocess.run(["git", "add", "-A"], cwd=out, capture_output=True)
    subprocess.run(["git", "-c", "user.name=panel", "-c", "user.email=panel@local",
                    "commit", "-qm", "interview kit"],
                   cwd=out, capture_output=True)

    if record:
        # The key to the payload, for the panel's bucket only. Written last, so a
        # build that failed leaves none behind.
        gate_out = pathlib.Path(gate_out)
        gate_out.parent.mkdir(parents=True, exist_ok=True)
        gate_out.unlink(missing_ok=True)          # O_CREAT's mode applies only to a new file
        fd = os.open(gate_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=1)

    print(f"built: {out}")          # the passphrase is never printed; the caller chose it
    if pack.tools:
        print(f"pack tools: {', '.join(t['name'] for t in pack.tools)}")
    print(f"sealed: {', '.join(pack.sealed)} plus problem.json"
          + (" and upload.json" if upload and upload.exists() else ""))
    if world is not None:
        print(f"world: {world}, sealed as data/")
    if record:
        print(f"gate: {gate} session {record['id']}, opens until {_iso(record['open_until'])}; "
              f"its record: {gate_out}")
    else:
        print("WARNING: no clock gate. The passphrase alone opens the exercise, with or without the clock.")
    if not (upload and upload.exists()):
        print("WARNING: no upload target. The submission will have nowhere to go.")
        print("  interview presign --endpoint ... --bucket ... --key ... -o upload.json")
    print("\nwhat the candidate can see before the session:")
    for p in sorted(out.rglob("*")):
        if p.is_file() and ".git/" not in str(p) and "__pycache__" not in str(p):
            print(f"  {p.relative_to(out)}")
    return out


# What every kit ships as .gitignore. `verify` compares a submission's against
# it: the recorder snapshots what git tracks, so a rule the candidate added is a
# rule about what the record contains.
GITIGNORE = [
    ".venv/", "__pycache__/", "*.pyc", "node_modules/", "/target/", "/dist/",
    ".mypy_cache/", ".pytest_cache/", ".ruff_cache/",
    "", "# interview tooling, leave this alone. The session's own files stay out of your",
    "# commits, and out of the way of git stash, checkout, reset and clean -fd.",
    ".interview/*", "!.interview/ivkit/", "!.interview/session.json",
    ".interview/ivkit/__pycache__/",
]
IGNORED_DIRS = (".venv", "__pycache__", "node_modules", "target", ".git", ".interview")

EXCLUDE = ["# interview session files; see .gitignore", ".interview/*", "!.interview/ivkit/",
           "!.interview/session.json", ".interview/ivkit/__pycache__/"]


def write_exclude(repo: pathlib.Path) -> None:
    """The same rules in .git/info/exclude, which no agent rewrites the way it
    might replace a .gitignore with a template. A clone does not copy it, so
    `interview start` writes it again."""
    info = repo / ".git" / "info"
    if not (repo / ".git").is_dir():
        return
    info.mkdir(parents=True, exist_ok=True)
    f = info / "exclude"
    have = f.read_text() if f.exists() else ""
    if ".interview/*" not in have:
        f.write_text(have.rstrip("\n") + ("\n" if have else "") + "\n".join(EXCLUDE) + "\n")


def _write_mcp_config(out: pathlib.Path) -> None:
    """Wires the panel and the hook log into the candidate's agent. These are
    the portable defaults; `interview setup` rewrites them for the candidate's machine."""
    from . import configs
    configs.write_defaults(out)


def unseal(repo: pathlib.Path, passphrase: str) -> dict:
    repo = pathlib.Path(repo).resolve()
    enc = repo / "payload.enc"
    if not enc.exists():
        meta = repo / "problem.json"
        if meta.exists():
            return json.loads(meta.read_text())
        raise SealError("payload.enc is missing. Tell the panel.")
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = pathlib.Path(tmp) / "payload.tar"
        r = subprocess.run([_openssl(), "enc", "-d", *[c for c in CIPHER if c != "-salt"],
                            "-in", str(enc), "-out", str(tar_path),
                            "-pass", "stdin"], input=passphrase + "\n", text=True, capture_output=True)
        if r.returncode != 0 or not tar_path.exists() or tar_path.stat().st_size == 0:
            raise SealError("That passphrase did not work. Check it with the panel.")
        with tarfile.open(tar_path) as tf:
            for m in tf.getmembers():
                if m.name.startswith(("/", "..")) or ".." in pathlib.PurePath(m.name).parts \
                        or not (m.isfile() or m.isdir()):
                    raise SealError(f"refusing to extract {m.name}")
            if hasattr(tarfile, "data_filter"):          # 3.12+: the library enforces the same
                tf.extractall(repo, filter="data")
            else:
                tf.extractall(repo)
    enc.unlink()
    meta = repo / "problem.json"
    return json.loads(meta.read_text()) if meta.exists() else {}


def _readme(pack, minutes: float, schedule: dict | None, every: float | None,
            open_until: str | None = None, pausable: bool = False, window_end: str | None = None) -> str:
    # Deliberately says nothing about which exercise this is. The title, the
    # brief and the data are all inside the sealed payload.
    if pausable:
        clock = (f"That opens the exercise, starts the recorder, and starts your {minutes:g} minutes of\n"
                 f"working time, which you can split across sittings:\n\n"
                 f"    ./interview pause      stops your clock\n"
                 f"    ./interview resume     starts it again\n\n"
                 f"Both need an internet connection. The clock keeps running unless you pause it,\n"
                 f"through a closed laptop too. Do not work on the exercise while paused: the\n"
                 f"recording shows what changed and when. Work on this computer, in this folder.\n\n"
                 f"Packaging and upload happen by themselves when your working time is up.\n"
                 f"Everything must be finished by {window_end} (UTC). Do not count\n"
                 f"on being submitted while paused: run `./interview submit` before then. If you\n"
                 f"are paused at that time, what is in this folder is submitted only if this\n"
                 f"computer is on, online and the recorder is running (after a restart, run\n"
                 f"`./interview status` once); otherwise only the checkpoints reach us.")
    elif schedule and schedule.get("deadline"):
        clock = (f"The session ends at {schedule['deadline']} (UTC), whenever you start: "
                 f"the panel's clock, not your computer's. Starting late shortens it. "
                 f"Packaging and upload happen by themselves at the end. Keep the computer "
                 f"awake and online until `./interview status` says submitted: the upload "
                 f"link closes {schedule.get('grace_minutes', 15):g} minutes after the end.")
    else:
        clock = (f"That opens the exercise, starts the recorder, and starts your {minutes:g} minutes.\n"
                 f"Packaging and upload happen by themselves when the time is up.")
    if open_until:
        clock += ("\n\nThe exercise is opened by the panel's server, which starts the clock the first\n"
                  "time it accepts the passphrase: `start` needs an internet connection, and\n"
                  "running it again later picks up the same clock rather than a new one. Run it\n"
                  "when you are ready to begin, not before."
                  + ("" if schedule and schedule.get("deadline") else
                     f" The server opens this session until\n{open_until} (UTC); start before then."))
    # On a paused kit `resume` starts the clock: after a sleep, `status` is the one to run.
    sleep = ("If your machine sleeps or restarts, the recorder comes back by itself the next\n"
             "time your agent or `./interview status` runs. Sleep does not stop your clock;\n"
             "only `./interview pause` does, and `./interview resume` is only for starting the\n"
             "clock again after `./interview pause`." if pausable else
             "If your machine sleeps or restarts, the recorder comes back by itself the next\n"
             "time your agent or `./interview status` runs; `./interview resume` does it\n"
             "directly. The deadline never moves.")
    cp = (f"\n\nEvery {every:g} minutes, a copy of the recording so far (the snapshots, your agent\n"
          "chat history for this folder and the session logs) is uploaded as a checkpoint. If\n"
          "your machine is offline it is sent when you reconnect." if every else "")
    # Said once, in the one file every candidate reads, before it can cost them
    # anything: the terminal message at the bell may go to a log instead.
    cp += ("\n\nWhat is not sent: build output. `dist/` and `target/` at the top of this folder, and\n"
           "`.venv/`, `__pycache__/` and `node_modules/` anywhere in it, are neither recorded nor\n"
           "packaged. Nor is any `.git` directory: your commits travel as a bundle of the whole\n"
           "history instead, so commit as much as you like. Keep work you want read outside those\n"
           "directories; everything else in this folder goes, whether or not git tracks it.")
    return f"""# Interview kit

Set this up before the session. It takes a minute and tells you nothing about the
exercise, which stays sealed until the panel gives you a passphrase.

## Before the day

```
./interview setup          # macOS, Linux, or WSL on Windows
```

It checks git, python and openssl{", reaches the panel's server" if open_until else ""}, wires the panel into your coding agent for
this machine, makes sure the session recorder runs here, and triggers any
operating-system permission prompts now rather than mid-session. If anything
fails, tell us before the day. On Windows, run everything inside WSL; native
Windows is not supported.

## On the day

```
./interview start <passphrase>
```

{clock}

Your agent can ask the panel directly: it has an `ask_panel` tool and a
`time_left` tool. From a terminal the same two are `./interview ask "..."` and
`./interview status`.

{sleep}

## What you may use

Any coding agent, model, editor, language or package manager. Some are recorded
more fully than others; see "Your tools". A cloud agent or a web chat keeps its
record on the provider's servers, not in this folder: as you go, record a share
link to each session, one anyone with it can open, with `./interview link <url>`.

## Your tools

Recorded in full: Claude Code and Codex. Recorded, but export each chat into
this folder before the end as well, as a backup: GitHub Copilot in VS Code, and
Cursor. Set up the one you will use before the day:

- Claude Code (the CLI or the VS Code extension): start it in this folder, not
  a parent folder. Trust the folder when it asks, and when it asks about the
  `interview-panel` server from `.mcp.json`, allow it; that is the panel.
- Codex (the CLI, the desktop app or the VS Code extension): run
  `./interview setup` first, then open Codex in this folder and trust the
  folder. Then approve the interview's hooks: `/hooks` in the CLI, or Settings >
  Hooks in the desktop app. Codex asks again whenever they change, so do this
  after setup. If you use the VS Code extension, also export each chat into this
  folder before the end. Work in this folder, not in a worktree.
- Copilot in VS Code: open this folder and choose Trust when VS Code asks. Use
  the Chat view with Local or Copilot as the session target. Export a chat with
  *Chat: Export Chat...*.
- Cursor (the editor, 3.22 or later, or `cursor-agent`): open this folder
  itself, and choose Enable when Cursor asks about the `interview-panel` MCP
  server. Run agents in this folder, not in a worktree or a best-of-N run. To
  redo a turn, send a new message. Export a chat from the chat's ... menu.

On the day, after your first prompt, `./interview status` shows hook records
for your tool; 0 means its hooks are not approved or not running.

Any other agent, editor or chat is allowed: export the chat into this folder
before the end, or record a share link with `./interview link <url>`. While a
tool we cannot read is in use, changes in this folder count as source unknown,
including lines you type yourself: they are not credited to you.

## What is recorded

A snapshot of the working tree every 20 seconds into
`.interview/snapshots.git`, which is separate from your own repository and never
touches your commits. At the end we gather the chat history your tools kept for
this project, and nothing from your other work.{cp}

All of this is done by Python under `.interview/`, plus the small hook and panel
files setup writes for each tool (in `.claude/`, `.codex/`, `.cursor/`,
`.github/hooks/` and `.vscode/`, and `.mcp.json`). Read them. Please do not
disable or remove them.
"""
