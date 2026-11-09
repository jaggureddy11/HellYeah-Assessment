"""Working-tree snapshots, the session deadline, checkpoints and the bell.

Tool-agnostic and OS-agnostic: it watches the filesystem, so it does not care
which agent, editor or language the candidate uses.

Snapshots go into a separate bare git directory, so the candidate's own
repository and commit history are never touched.

One loop per session. It holds an exclusive lock for as long as it runs, so
however many things try to restart it (the agent's panel server, the Claude
Code hook, `interview status`, `interview resume`), at most one records.
Whoever holds that lock is also the only one allowed to package the
submission. A submission is frozen once; it is frozen again only if that
archive has disappeared from disk, and the manifest then says so. A failed
upload is retried with the same archive. The protocol, and the ways the simpler
versions of it fail, are model-checked in platform/formal/session.

Liveness is a heartbeat file, written by a thread for as long as the lock is
held. It only decides when to try a restart; the lock decides who runs.

A take-home runs on the storage server's clock: the deadline and the
checkpoint times in recorder.json are server time, and this machine's offset
from it is measured at start and again from every upload's reply, and in a
pausable kit from every answer of the clock gate too (note_gate_offset).
"""
from __future__ import annotations
import errno, hashlib, json, os, shutil, subprocess, sys, threading, time, pathlib, datetime

INTERVAL = float(os.environ.get("INTERVIEW_TICK") or 20)   # the variable is for tests only
STALE_AFTER = INTERVAL * 4.5    # heartbeat older than this means the recorder died
RETRY_EVERY = INTERVAL * 3      # how often a failed upload is tried again
GIT_TIMEOUT = 60                # a wedged git must not wedge the lock holder
# A pausable kit (pause.py): how often the recorder reads the gate's clock, so
# that a pause or resume whose answer was lost still counts (spec §5.2); and how
# long a running clock sees no work before one reminder. The variables are for
# tests only.
SYNC_EVERY = float(os.environ.get("INTERVIEW_SYNC") or 120)
IDLE = float(os.environ.get("INTERVIEW_IDLE") or 900)
# What a sync that moved the phase tells the candidate (spec §5.2), with the
# time the server recorded the move at, on this machine's clock.
MOVED = {"running": "Your clock is running: the panel's server recorded a resume at {at}. ./interview status "
                    "shows the time left.",
         "paused": "Your clock is paused: the panel's server recorded a pause at {at}. ./interview resume "
                   "starts it again."}
WARNINGS = ((600, "10 minutes left."), (60, "1 minute left. Packaging and upload are automatic."))
IDLE_TEXT = "No activity for 15 minutes. Taking a break? ./interview pause stops your clock."
# `/dist/` and `/target/` are anchored: at the repository root they are build
# output, and submit leaves them out of the archive for the same reason. A
# `docs/dist/` is the candidate's writing and is both snapshotted and shipped.
EXCLUDES = [".venv/", "__pycache__/", "node_modules/", "/target/", "/dist/",
            ".mypy_cache/", ".pytest_cache/", ".ruff_cache/",
            ".interview/snapshots.git/", ".interview/recorder.lock", ".interview/submit-requested",
            ".interview/recorder.log", ".interview/*.tmp", "*.pyc", "submission-*.tar.gz*",
            # A pausable kit keeps its passphrase in gate.json so that pause and resume
            # take no argument; it must never reach the record (spec §5.1). The kit's
            # .gitignore hides .interview/ too, but an agent may replace that file, and
            # then these lines are all that keep it out. The ledger's lock and pause's
            # snapshot handshake (snapshot-requested, snapshot-done.json) are the kit's
            # plumbing, not the candidate's work.
            ".interview/gate.json", ".interview/ledger.lock", ".interview/ledger.lock.d/",
            ".interview/snapshot-requested", ".interview/snapshot-done.json"]


def iv_dir(repo: pathlib.Path) -> pathlib.Path:
    return repo / ".interview"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(args, 1, "", repr(e))


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    snap = iv_dir(repo) / "snapshots.git"
    return _run(["git", f"--git-dir={snap}", *args])


def _git_wt(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    snap = iv_dir(repo) / "snapshots.git"
    return _run(["git", f"--git-dir={snap}", f"--work-tree={repo}", *args])


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime.datetime:
    """fromisoformat on Python 3.9 does not take a trailing Z."""
    return datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)


def mirror_dir(repo: pathlib.Path) -> pathlib.Path | None:
    """A second home for the files the session cannot run without (the clock
    and the upload links), inside the repository's .git where no clean, stash,
    checkout or reset reaches. None if the repository has no .git."""
    g = repo / ".git"
    return g / "interview" if g.is_dir() else None


def mirror(repo: pathlib.Path, name: str) -> None:
    m = mirror_dir(repo)
    src = iv_dir(repo) / name
    if m is None or not src.exists():
        return
    try:
        m.mkdir(parents=True, exist_ok=True)
        tmp = m / (name + ".tmp")
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, m / name)
    except OSError:
        pass                        # the second copy is best effort; the first still stands


def restore(repo: pathlib.Path, name: str) -> bool:
    """Puts a vital file back from the mirror if it was deleted (git clean -x)."""
    m = mirror_dir(repo)
    dst = iv_dir(repo) / name
    if dst.exists() or m is None or not (m / name).exists():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(name + ".restoring.tmp")
        tmp.write_bytes((m / name).read_bytes())
        os.replace(tmp, dst)                     # readers never see half of it
    except OSError:
        return False
    event(repo, "restored", file=name, detail="deleted from .interview/, put back from .git/interview/")
    return True


def state(repo: pathlib.Path) -> dict:
    f = iv_dir(repo) / "recorder.json"
    if not f.exists() and not restore(repo, "recorder.json"):
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _write_state(repo: pathlib.Path, st: dict) -> None:
    f = iv_dir(repo) / "recorder.json"
    tmp = f.with_name("recorder.json.tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, f)
    if not st.get("selftest"):
        mirror(repo, "recorder.json")


def started(repo: pathlib.Path) -> bool:
    """A real session has begun here: the clock is set, or the exercise has been
    unsealed. The second test survives losing recorder.json."""
    st = state(repo)
    if st.get("deadline") and not st.get("selftest"):
        return True
    return not (repo / "payload.enc").exists() and (repo / "problem.json").exists() \
        and (iv_dir(repo) / "ivkit" / "_role").exists()


def offset(repo: pathlib.Path, st: dict) -> float:
    """Seconds to add to this machine's clock to get the storage server's, from
    clock.json (the latest upload reply, or in a pausable kit the best recent
    gate answer: note_gate_offset), else from the measurement at start."""
    try:
        return float(json.loads((iv_dir(repo) / "clock.json").read_text())["offset"])
    except Exception:
        return float(st.get("clock_offset", 0))


def now(repo: pathlib.Path, st: dict) -> float:
    """Now, on the clock the session's times are written in."""
    return time.time() + (offset(repo, st) if st.get("clock") == "server" else 0.0)


def keeps_gate_offset(repo: pathlib.Path, off: float, err: float) -> bool:
    """Whether clock.json holds a gate answer's measurement of the offset that
    is to stay rather than give way to `off`, a measurement within `err` of the
    truth: it is more precise, and the two agree within both their errors, so
    the machine's clock has not moved between them. Only a pausable kit writes
    a gate measurement (note_gate_offset), so for any other kit this is False,
    and its uploads keep clock.json as they always have (submit.note_clock).

    Never raises, as offset() does not: clock.json is in the candidate's folder,
    and one that cannot be read (nested past the parser's depth raises
    RecursionError) keeps nothing, and is overwritten as it always was."""
    try:
        cur = json.loads((iv_dir(repo) / "clock.json").read_text())
        return (cur.get("source") == "gate" and float(cur["err"]) < err
                and abs(off - float(cur["offset"])) <= err + float(cur["err"]))
    except Exception:
        return False


def note_gate_offset(repo: pathlib.Path, off: float, err: float) -> None:
    """Keeps a pausable kit's offset current from a gate answer: every one
    carries the server's `now` (spec §4.2), and `err` is half the time the
    request took. The machine's clock can be corrected between sittings, days
    apart, and the recorder times the bell with offset(): the offset kept from
    the first sitting would ring it early, or late, by the correction. The
    caller (pause.apply) writes this before it moves recorder.json's deadline,
    so the recorder never times a new deadline with an old offset.

    The gate's `now` is to the millisecond, an upload's Date header to the
    second and stamped when the whole archive has arrived. So a measurement
    gives way only to one at least as precise, or to one it does not agree with
    (keeps_gate_offset), which means the clock has moved. Written whole and
    renamed into place, under a name of its own: the recorder and a command can
    write at once, and record.offset must never read half a file."""
    if keeps_gate_offset(repo, off, err):
        return
    f = iv_dir(repo) / "clock.json"
    tmp = f.with_name(f"clock.json.{os.getpid()}-{os.urandom(4).hex()}.tmp")      # *.tmp: never snapshotted
    try:
        tmp.write_text(json.dumps({"offset": off, "err": err, "at": time.time(), "source": "gate"}))
        os.replace(tmp, f)
    except OSError:
        # The offset in force still stands, and the next answer tries again.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            event(repo, "clock_note_error", file=tmp.name)


def time_left(repo: pathlib.Path, st: dict) -> float:
    return st.get("deadline", float("inf")) - now(repo, st)


def alive(repo: pathlib.Path) -> bool:
    hb = iv_dir(repo) / "heartbeat"
    try:
        return (time.time() - hb.stat().st_mtime) < STALE_AFTER
    except OSError:
        return False


def snapshot_count(repo: pathlib.Path) -> int:
    r = _git(repo, "rev-list", "--count", "HEAD")
    try:
        return int(r.stdout.strip())
    except Exception:
        return 0


def head(repo: pathlib.Path) -> str:
    """The latest snapshot's commit, or "" before the first."""
    r = _git(repo, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


class _NoLock:
    """Returned when this filesystem cannot lock at all (some network or FUSE
    mounts). Recording without a lock beats not recording."""
    logged = False

    def close(self):
        pass


def _lock_files(repo: pathlib.Path) -> list[pathlib.Path]:
    """Every place the lock lives. A holder holds all of them, and a newcomer
    must get all of them: `git clean -x` removes the one under .interview/,
    `rm -rf .git` the one under .git/, and neither alone lets a second
    recorder in, because the other is still held."""
    m = mirror_dir(repo)
    return ([m / "recorder.lock"] if m else []) + [iv_dir(repo) / "recorder.lock"]


class _Fd:
    def __init__(self, fd: int):
        self.fd = fd

    def close(self):
        os.close(self.fd)


def dir_lock(path: pathlib.Path):
    """flock on a directory itself: an object to close, None if held, or
    "unsupported". A directory's inode outlives everything done inside it:
    git clean, stash, checkout, reset, even rm -rf .git. A moved folder keeps
    it, a copy gets its own."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return "unsupported"
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _Fd(fd)
    except ImportError:
        os.close(fd)
        return "unsupported"
    except OSError as e:
        os.close(fd)
        return None if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES) else "unsupported"


class _Held:
    def __init__(self, files):
        self.files = files

    def close(self):
        for f in self.files:
            try:
                f.close()
            except OSError:
                pass                # the kernel drops the lock with the descriptor regardless


def _flock(path: pathlib.Path):
    """An open file holding the lock, None when someone else holds it, or
    "unsupported" when this filesystem cannot lock."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
    except OSError:
        return "unsupported"
    try:
        try:
            import fcntl
        except ImportError:                       # native Windows, unsupported
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError as e:
        f.close()
        return None if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES) else "unsupported"


def try_lock(repo: pathlib.Path):
    """The session lock: exclusive, never waits. Returns an object whose
    lifetime is the lock (close() releases it), or None when someone else holds
    it. The kernel drops it when the holder exits, however it exits, so a crash
    never leaves it stuck. Child processes do not inherit it (Popen closes fds)."""
    iv_dir(repo).mkdir(parents=True, exist_ok=True)
    d = dir_lock(repo)
    if d is None:
        return None
    if d != "unsupported":
        return _Held([d])
    # A filesystem that cannot lock a directory: lock files, in both places.
    got = []
    for path in _lock_files(repo):
        f = _flock(path)
        if f is None:
            for g in got:
                g.close()
            return None                           # held by someone else
        if f != "unsupported":
            got.append(f)
    if got:
        return _Held(got)
    key = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:16]
    f = _flock(pathlib.Path.home() / ".cache" / "interview" / f"{key}.lock")
    if f is None:
        return None
    if f != "unsupported":
        return _Held([f])
    marker = iv_dir(repo) / "no_lock"
    if not _NoLock.logged and not marker.exists():
        _NoLock.logged = True
        event(repo, "no_lock", detail="no lockable file; recording without a lock")
        try:
            marker.write_text("1")
        except OSError:
            pass                    # the marker only stops repeat events; this one was logged
    return _NoLock()


def event(repo: pathlib.Path, name: str, **fields) -> None:
    """Appends one line to .interview/recorder_events.jsonl: when the recorder
    came up, who brought it back, and what went wrong, so a gap in the
    snapshots has a cause."""
    try:
        with (iv_dir(repo) / "recorder_events.jsonl").open("a") as f:
            f.write(json.dumps({"ts": utc_now(), "event": name, **fields}) + "\n")
    except OSError:
        pass                        # the event log is best effort: recording goes on without it


def _init_snapshots(repo: pathlib.Path) -> None:
    d = iv_dir(repo)
    snap = d / "snapshots.git"
    if not (snap / "HEAD").exists():
        subprocess.run(["git", f"--git-dir={snap}", "init", "-q", "--bare"], capture_output=True)
    (d / "snapshot-exclude").write_text("\n".join(EXCLUDES) + "\n")
    _git(repo, "config", "core.excludesFile", str(d / "snapshot-exclude"))
    _git(repo, "config", "gc.auto", "0")       # no repacking under a checkpoint's copy


def start(repo: pathlib.Path, minutes: float = 60.0, deadline: float | None = None,
          checkpoint_base: float | None = None, clock_offset: float = 0.0,
          offset_measured: bool = False, selftest: bool = False) -> str:
    """Starts the clock and the loop. With `deadline` (server epoch seconds),
    the session runs on the panel's clock; otherwise it ends `minutes` from now
    on this machine's."""
    d = iv_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    if alive(repo):
        return "already recording"
    _init_snapshots(repo)
    t = time.time()
    server = deadline is not None
    st = {"started": t, "clock": "server" if server else "local",
          "deadline": deadline if server else t + minutes * 60,
          "checkpoint_base": (checkpoint_base if checkpoint_base is not None else t + clock_offset)
          if server else t,
          "minutes": minutes, "clock_offset": clock_offset, "offset_measured": offset_measured,
          "selftest": selftest, "repo": str(repo.resolve())}
    _write_state(repo, st)
    if not selftest:
        mirror(repo, "upload.json")
    for f in ("submitted.json", "submit-requested", "stop", "clock.json"):
        (d / f).unlink(missing_ok=True)
    if not selftest:
        event(repo, "start", clock=st["clock"], deadline=datetime.datetime.fromtimestamp(
            st["deadline"], datetime.timezone.utc).isoformat(timespec="seconds"))

    _spawn_loop(repo, "start")

    for _ in range(15):
        if snapshot_count(repo) >= 1:
            break
        time.sleep(1)
    left = f"{time_left(repo, st) / 60:.0f}" if server else f"{minutes:g}"
    return f"recording, snapshot every {INTERVAL:g}s, deadline in {left} minutes"


def _same_session(repo: pathlib.Path, st: dict) -> bool:
    """recorder.json names the directory the session was started in. A copy of
    that directory is not the session: it gets no recorder. A moved one is: the
    old path is gone, so adopt the new one."""
    here = str(repo.resolve())
    there = st.get("repo")
    if not there or there == here:
        return True
    try:
        if os.path.samefile(there, here):      # the same folder, spelled differently
            return True
    except OSError:
        pass                        # one of the two is gone: decided by existence below
    if pathlib.Path(there).exists():
        return False
    st["repo"] = here
    try:
        _write_state(repo, st)
    except OSError:
        pass                        # the move is adopted in memory; the next tick writes it again
    event(repo, "moved", previous=there)
    return True


def ensure(repo: pathlib.Path, by: str) -> str | None:
    """The watchdog. Brings the loop back when a started session has no live
    recorder and still needs one: to keep recording, to submit at the bell, or
    to retry a failed upload. Cheap, silent and safe to call from anywhere, as
    often as anyone likes: the lock makes a second loop exit at once."""
    try:
        st = state(repo)
        if not st.get("deadline") or st.get("selftest"):
            return None
        if alive(repo):
            # A fresh heartbeat can be a recorder that was killed seconds ago:
            # the lock says whether anyone really holds the session.
            probe = try_lock(repo)
            if probe is None or isinstance(probe, _NoLock):
                return None
            probe.close()
        if not _same_session(repo, st):
            return None
        from . import submit
        r = submit.receipt(repo)
        if r.get("upload") in submit.DONE or r.get("upload") in submit.GIVEN_UP:
            return None
        if r and submit.expired(repo):
            return None
        _spawn_loop(repo, by)
        return "restarted"
    except Exception:
        return None


def resume(repo: pathlib.Path) -> str:
    st = state(repo)
    if not st.get("deadline") or st.get("selftest"):
        return "nothing to resume: the session was never started here"
    if alive(repo):
        return "already recording"
    if ensure(repo, "resume") is None:
        return status(repo)
    for _ in range(int(STALE_AFTER) + 5):
        if alive(repo):
            break
        time.sleep(0.5)
    left = time_left(repo, st)
    if left <= 0:
        return "the deadline has passed; the recorder is packaging and uploading now"
    return f"resumed, {int(left) // 60}m {int(left) % 60}s left on the original clock"


def _spawn_loop(repo: pathlib.Path, by: str) -> None:
    kw: dict = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    pkg_parent = str(pathlib.Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    log = open(iv_dir(repo) / "recorder.log", "a")
    try:
        subprocess.Popen([sys.executable, "-m", "ivkit.record", "_loop", str(repo.resolve()), by],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env, **kw)
    finally:
        log.close()


# Spec §5.2: what a pausable kit says of its clock when the sync just made got no
# answer with a clock this kit could read: none at all, or one it could not take
# in. The copy can be behind the server (a pause or resume whose answer was
# lost), so it is said as the last known here, never as the server's; with
# nothing to show, that the server has not said.
NO_CLOCK = "the panel's server did not answer with a clock this kit could read"
LAST_KNOWN = NO_CLOCK + (", so this is your clock as last known on this machine, and a pause or resume may "
                         "have landed since: {clock}")


def status(repo: pathlib.Path, heard: bool = True) -> str:
    """The session's state, for `status` and the agent's time_left. For a
    pausable kit, `heard` is whether the sync those just made got an answer with
    a clock this kit could read; whenever it did not, what is said of the clock
    is labelled LAST_KNOWN (or NO_CLOCK, with nothing to show)."""
    from . import submit
    st = state(repo)
    r = submit.receipt(repo)
    if r:
        up = r.get("upload")
        on_disk = bool(r.get("archive")) and pathlib.Path(r["archive"]).exists()
        where = f"The archive is at {r.get('archive')}." if on_disk else "The archive is no longer on disk."
        if up in submit.DONE:
            return f"finished and submitted ({up}), sha256 {r.get('sha256', '')[:16]}"
        if up in submit.GIVEN_UP:
            return f"finished, NOT uploaded ({up}). {where} Tell the panel."
        return (f"finished at {r.get('frozen_at')}; the upload has not landed yet ({up}). "
                f"The recorder keeps retrying. {where}")
    if st.get("repo") and not _same_session(repo, st):
        return (f"This folder is a copy of the session folder {st['repo']}; nothing is recorded "
                f"here. Work there.")
    # A pausable kit's time left is the budget's, from the ledger, not the
    # deadline less now: while paused the deadline is the window's end (spec §5.1).
    # `status` and the agent's time_left have just synced it (spec §5.3), so it
    # is said even when nothing records: as the server's when the server
    # answered, else as the last known here (spec §5.2).
    from . import pause
    led = pause.load(repo) if pause.session(repo) else {}
    clock = pause.describe(led, now(repo, st), offset(repo, st)) if led else ""
    if not alive(repo):
        if not heard:
            return "NOT RECORDING; " + (LAST_KNOWN.format(clock=clock) if clock else NO_CLOCK)
        return "NOT RECORDING" + (f"; your clock is {clock}" if clock else "")
    left = max(int(time_left(repo, st)), 0)
    from . import checkpoint
    cp = checkpoint.summary(repo)
    # An empty ledger (its first clock never readable) shows recorder.json's
    # deadline, which is just as much the last known here.
    shown = clock or f"{left // 60}m {left % 60}s left"
    when = f", {shown}" if heard else f"; {LAST_KNOWN.format(clock=shown)}"
    return f"recording, {snapshot_count(repo)} snapshots{when}" + (f", {cp}" if cp else "")


def stop(repo: pathlib.Path) -> str:
    """Only for `interview setup`'s self-test. A session in progress is ended
    by submitting, never by stopping the recorder."""
    (iv_dir(repo) / "stop").write_text("1")
    for _ in range(int(INTERVAL) + 5):
        if not alive(repo):
            break
        time.sleep(1)
    return f"recording stopped, {snapshot_count(repo)} snapshots"


class _Beat(threading.Thread):
    """Writes the heartbeat for as long as the lock is held, so a long upload
    or packaging step does not look like a dead recorder."""

    def __init__(self, hb: pathlib.Path):
        super().__init__(daemon=True)
        self.hb, self.halt = hb, threading.Event()

    def run(self):
        last = time.time()
        while not self.halt.is_set():
            t = time.time()
            if t - last > STALE_AFTER:           # the machine slept, or this process was stopped
                event(self.hb.parent.parent, "slept", seconds=round(t - last))
            last = t
            try:
                self.hb.write_text(str(t))
            except OSError:
                pass                # a missed beat only prompts a restart attempt, which the lock refuses
            self.halt.wait(max(INTERVAL / 2, 0.2))

    def end(self):
        self.halt.set()
        self.join(timeout=2)
        self.hb.unlink(missing_ok=True)


def _snapshot(repo: pathlib.Path, errors: dict) -> None:
    if not (iv_dir(repo) / "snapshots.git" / "HEAD").exists():
        _init_snapshots(repo)
        event(repo, "snapshots_reinitialized", detail="snapshots.git was missing; earlier "
              "snapshots survive only in checkpoints")
    add = _git_wt(repo, "add", "-A", "--ignore-errors")
    # The subject carries the recorder's estimate of the server's clock, which
    # `verify` cuts on: this machine's clock may move during the session.
    server = time.time() + offset(repo, state(repo))
    _git_wt(repo, "-c", "user.name=interview", "-c", "user.email=interview@local",
            "commit", "-q", "-m",
            "snapshot " + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            + f" s={server:.0f}")
    msg = add.stderr.strip()[:300] if add.returncode != 0 else ""
    if msg != errors.get("last"):
        errors["last"] = msg
        if msg:
            event(repo, "snapshot_error", detail=msg)


def _answer_snapshot(repo: pathlib.Path, frozen: bool, errors: dict) -> None:
    """`pause` and `resume` ask for a snapshot of the work as it is now
    (pause.snapshot, spec §5.3). Only the lock holder snapshots, so they ask,
    and this answers with the commit. After the bell nothing more is
    snapshotted: the answer is then the last one.

    The request is claimed first, by renaming it to a name of its own. A
    request written after that is a new file, which the next pass answers;
    read in place and deleted after answering, a second command's request
    written in between would be deleted unanswered, and that command would
    wait out its whole wait for nothing."""
    iv = iv_dir(repo)
    claim = iv / f"snapshot-requested.claim-{os.urandom(4).hex()}.tmp"   # *.tmp: never snapshotted
    try:
        os.replace(iv / "snapshot-requested", claim)
    except FileNotFoundError:
        errors.pop("request_stuck", None)
        return
    except OSError as e:
        # It cannot even be moved (.interview/ is not writable). Said once;
        # _nap then sleeps through it rather than waking for it at once, for good.
        if not errors.get("request_stuck"):
            errors["request_stuck"] = True
            event(repo, "snapshot_request_error", error=repr(e)[:300])
        return
    try:
        token = claim.read_text().strip()
    except (OSError, ValueError) as e:
        # A directory, a file this process cannot read, or not text: nothing
        # pause.snapshot wrote. Moved away above, so it cannot wake the loop
        # again; removed here, and logged.
        if claim.is_dir():
            shutil.rmtree(claim, ignore_errors=True)
        else:
            claim.unlink(missing_ok=True)
        event(repo, "snapshot_request_error", error=repr(e)[:300])
        return
    if not frozen:
        _snapshot(repo, errors)
    from . import submit
    submit._write_json(iv / "snapshot-done.json", {"token": token, "head": head(repo)})
    claim.unlink(missing_ok=True)


def _nap(repo: pathlib.Path, seconds: float, wake: bool = True) -> None:
    """The loop's sleep, cut short when a command asks for a snapshot: the
    candidate waits on `pause` for it, not for the next tick. wake=False for a
    request that cannot be claimed (_answer_snapshot), which would otherwise cut
    every sleep short."""
    end = time.time() + seconds
    req = iv_dir(repo) / "snapshot-requested"
    while not (wake and req.exists()):
        rest = end - time.time()
        if rest <= 0:
            return
        time.sleep(min(0.25, rest))


def _hooked(d: pathlib.Path) -> float:
    """When an agent's hook last wrote, by the hook log's mtime: work that is
    not a file change yet."""
    try:
        return (d / "transcript.jsonl").stat().st_mtime
    except OSError:
        return 0.0


def _worked(repo: pathlib.Path, before: str, after: str) -> bool:
    """Whether the snapshot `after` holds work that `before` did not: a change
    outside .interview/. The kit's .gitignore keeps .interview/ out of the
    snapshots, but an agent's template may replace it, and then the kit's own
    files (the heartbeat, the ledger, the event log, the idle reminder's own
    line too) change on every tick. They are not the candidate's activity
    (spec §5.4). A first snapshot, or two that cannot be compared (snapshots.git
    was lost and begun again), count as work: a reminder held back is the safe
    mistake.

    Read as bytes: with -z git prints each path as it is on disk, and Linux and
    WSL allow a name that is not text in this machine's encoding. Decoded, such
    a name would raise on every tick, and with the tick the bell."""
    if not before:
        return True
    snap = iv_dir(repo) / "snapshots.git"
    try:
        r = subprocess.run(["git", f"--git-dir={snap}", "diff-tree", "-r", "-z", "--name-only", "--no-renames",
                            before, after], capture_output=True, timeout=GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return True
    if r.returncode != 0:
        return True
    return any(n and not n.startswith(b".interview/") for n in r.stdout.split(b"\0"))


def _sync(repo: pathlib.Path, timeout: float = 5.0) -> None:
    """One read of a pausable kit's clock from the gate (pause.sync_heard), which
    also points recorder.json's deadline at the bell. A sync that changed the
    phase is logged and told to the candidate, the one just before the bell
    too (spec §5.2): it is how they learn of a pause or resume whose answer
    was lost, which the command could only say it did not know about.

    With no answer, recorder.json is still brought to the bell of the kit's
    own ledger (pause.settle): the ledger is the copy of the server's clock,
    and recorder.json follows it (spec §5.1). A sync that fails is a sync
    with no answer: logged, and the bell decides on the deadline as it
    stands. Raised, it would cost the loop its error back-off on every tick
    at the bell, and so put the bell off for as long as the fault lasts."""
    from . import paths, pause
    try:
        st, moved, heard = pause.sync_heard(repo, timeout)
        if not heard:
            pause.settle(repo)
        if moved:
            event(repo, "clock_moved", phase=moved)
            # The move the server recorded: the latest pause or resume. A ledger
            # filled in for the first time (its first clock could not be read at
            # start) can move to running 1, which no resume made, and the recorder
            # was already running on that clock: nothing to tell.
            at = max((e["at"] for e in st.get("events") or []), default=None)
            if at is not None:
                paths.notify(MOVED[moved].format(at=pause._local(at, offset(repo, state(repo)))))
    except Exception as e:
        event(repo, "sync_error", error=repr(e)[:300])


def _warnings(left: float, seg, warned: set, pausable: bool) -> list:
    """The time warnings due now, each given once per segment (spec §5.1): a
    resume starts a segment with a deadline of its own, which warns again. A kit
    that cannot pause has one segment, (None, None), and warns as it always has.
    A pausable segment can begin with less than ten minutes of the budget left
    (a resume late in it): that one is told the time it has, and under a
    minute only the last warning, rather than "10 minutes left" at once."""
    out = []
    for mark, msg in WARNINGS:
        if left > mark or (seg, mark) in warned:
            continue
        warned.add((seg, mark))
        if pausable and mark == 600 and left < 540:
            if left <= 60:
                continue                    # the one-minute warning, next, is the one to give
            msg = f"{int(left) // 60}m {int(left) % 60:02d}s left."
        out.append(msg)
    return out


class _Minder:
    """A tick's bookkeeping once the bell has decided not to ring: the time
    warnings, and a pausable kit's idle reminder (spec §5.1, §5.4). It must never
    stand between a tick and the bell, the submission or a checkpoint, so tick()
    never raises. A step that fails is logged once (bookkeeping_error; again
    only after it has worked in between) and counts as work: no reminder rests
    on a guess. A kit that cannot pause gets the warnings as before, and no git
    call and no ledger read."""

    def __init__(self, repo: pathlib.Path, pausable: bool):
        self.repo, self.pausable = repo, pausable
        self.warned: set = set()            # (segment, mark): each segment warns again
        self.seg = None                     # the segment last seen, (phase, n)
        self.head = None                    # the snapshot last looked at; None before the first look
        self.work = time.time()             # when the candidate's work last showed
        self.told_for = -1.0                # the activity time the last reminder was for
        self.faults: dict = {}

    def _fault(self, step: str, e: BaseException | None) -> None:
        if e is None:
            self.faults.pop(step, None)
            return
        self.work = time.time()
        msg = repr(e)[:300]
        if self.faults.get(step) != msg:
            self.faults[step] = msg
            event(self.repo, "bookkeeping_error", step=step, error=msg)

    def tick(self, left: float) -> None:
        try:
            self._tick(left)
            self._fault("tick", None)
        except Exception as e:
            self._fault("tick", e)

    def _tick(self, left: float) -> None:
        from . import paths, pause
        led: dict | None = {}
        if self.pausable:
            try:
                led = pause.load(self.repo)
                self._fault("ledger", None)
            except Exception as e:
                self._fault("ledger", e)
                led = None                  # the phase is unknown: warned by the deadline as it stands
        if led is not None and (led.get("phase"), led.get("n")) != self.seg:
            # A new segment, or a pause: the idle count starts again.
            self.seg, self.work = (led.get("phase"), led.get("n")), time.time()
        if self.pausable:
            try:
                h = head(self.repo)
                if h != self.head:
                    if self.head is None or _worked(self.repo, self.head, h):
                        self.work = time.time()
                    self.head = h
                self._fault("snapshots", None)
            except Exception as e:
                self._fault("snapshots", e)
        if led and led.get("phase") != "running":
            return                          # paused: the bell is the window's end, not the time left
        for msg in _warnings(left, self.seg or (None, None), self.warned, self.pausable):
            paths.notify(msg)
        if not self.pausable:
            return
        # Spec §5.4: while running, IDLE with no new snapshot of the candidate's
        # work and no hook record brings one reminder, and none again until there
        # has been activity: then the activity time is later than the one the last
        # reminder was for. Both times are this machine's: the hook log's mtime and
        # self.work.
        active = max(self.work, _hooked(iv_dir(self.repo)))
        if active > self.told_for and time.time() - active >= IDLE:
            self.told_for = active
            event(self.repo, "idle_reminder")
            paths.notify(IDLE_TEXT)


def _loop(repo: pathlib.Path, by: str = "start") -> None:
    held = try_lock(repo)
    if held is None:
        return                              # another loop holds the session
    d = iv_dir(repo)
    hb = d / "heartbeat"
    cached = state(repo)
    if not cached.get("deadline") or not _same_session(repo, cached):
        return
    if not cached.get("selftest"):
        try:
            gap = round(time.time() - hb.stat().st_mtime)
        except OSError:
            gap = None
        event(repo, "recorder_up", by=by, since_last_heartbeat_s=gap)
    beat = _Beat(hb)
    beat.start()
    try:
        _run_loop(repo, held, cached)
    finally:
        beat.end()
        from . import checkpoint
        checkpoint.abandon(repo)


def _run_loop(repo: pathlib.Path, held, cached: dict) -> None:
    from . import paths, submit, checkpoint
    d = iv_dir(repo)
    errors: dict = {}
    last_try = 0.0
    rang = False
    links = d / "upload.json"
    links_cache = links.read_bytes() if links.exists() else None   # a third copy, in memory
    from . import pause
    try:
        pausable = bool(pause.session(repo))
    except Exception:
        # session.json that cannot even be parsed (nested past the parser's depth):
        # no pausable clock to mind. recorder.json's deadline still rings the bell.
        pausable = False
    last_sync = time.time()
    minder = _Minder(repo, pausable)
    while True:
        try:
            if not repo.exists():
                return                      # the session folder was deleted: nothing left to record
            if (d / "stop").exists():
                (d / "stop").unlink(missing_ok=True)
                if cached.get("selftest"):
                    return
                event(repo, "stop_ignored", detail="a session in progress is ended by submitting")
            st = state(repo)
            if not st.get("deadline"):
                st = cached                 # recorder.json was deleted: put it back
                _write_state(repo, st)
                event(repo, "state_restored", detail="recorder.json was missing")
            cached = st
            if links_cache is not None and not links.exists() and not restore(repo, "upload.json"):
                links.write_bytes(links_cache)   # both copies gone at once: put back what we hold
                event(repo, "restored", file="upload.json", detail="from the recorder's memory")
            m = mirror_dir(repo)
            for name in ("recorder.json", "upload.json"):
                if m is not None and not (m / name).exists():
                    mirror(repo, name)       # .git was replaced: keep the second copy
                    from . import seal
                    seal.write_exclude(repo)
            r = submit.receipt(repo)
            up = r.get("upload")
            _answer_snapshot(repo, rang or bool(r), errors)
            if up in submit.DONE or up in submit.GIVEN_UP:
                return
            if r:
                # Frozen already. Nothing more is recorded; the upload has to land.
                asked = (d / "submit-requested").exists()
                on_disk = bool(r.get("archive")) and pathlib.Path(r["archive"]).exists()
                if not on_disk:
                    event(repo, "archive_missing", archive=r.get("archive"))
                    res = submit.run(repo, holder=held)      # freezes again, and says so
                    last_try = time.time()
                elif submit.expired(repo):
                    event(repo, "upload_gave_up", upload=up)
                    submit.give_up(repo)
                    paths.notify("UPLOAD FAILED and the link has expired. Tell the panel.")
                    return
                elif asked or time.time() - last_try >= RETRY_EVERY:
                    res = submit.retry(repo)
                    last_try = time.time()
                    (d / "submit-requested").unlink(missing_ok=True)
                else:
                    res = r
                if res.get("upload") in submit.DONE:
                    paths.notify("Submitted.")
                    return
            else:
                # Once the bell has rung its decision is final (spec §5.2): no sync
                # after it, though packaging may be retrying here.
                synced = pausable and not rang and time.time() - last_sync >= SYNC_EVERY
                if synced:
                    last_sync = time.time()
                    _sync(repo)
                    st = state(repo) or st
                left = time_left(repo, st)
                if not rang:
                    _snapshot(repo, errors)      # none after the bell: packaging may be retrying
                asked = (d / "submit-requested").exists()
                if pausable and not rang and not synced and left <= 0 and not asked and not st.get("selftest"):
                    # A pause or resume whose answer was lost counts before the bell
                    # (spec §5.2). One short try; then the bell's decision is final:
                    # once it has rung (rang, and packaging may be retrying here) no
                    # sync moves it again. Not twice in one tick: a periodic sync just
                    # now (synced) was that try.
                    last_sync = time.time()
                    _sync(repo, timeout=5)
                    st = state(repo) or st
                    left = time_left(repo, st)
                # A pausable kit's bell, once rung, rings until a receipt exists:
                # final (spec §5.2), even if `status` or the agent's time_left has
                # since applied a clock that moves recorder.json's deadline on.
                if not st.get("selftest") and (left <= 0 or asked or (pausable and rang)):
                    rang = True
                    checkpoint.abandon(repo)        # the bell does not wait for a checkpoint
                    paths.notify("Time. Packaging and uploading now." if left <= 0
                                 else "Packaging and uploading your submission.")
                    res = submit.run(repo, holder=held)
                    last_try = time.time()
                    if res.get("upload") in submit.DONE:
                        paths.notify("Submitted.")
                        return
                    event(repo, "upload_failed", upload=res.get("upload"), error=submit.last_error)
                    paths.notify("UPLOAD FAILED. Nothing is lost; it keeps retrying. Tell the panel.")
                elif not st.get("selftest"):
                    checkpoint.tick(repo, st)
                    minder.tick(left)
        except Exception as e:
            # Nothing may end the loop while the session still needs it: log,
            # wait, try again. A persistent fault shows in recorder_events.jsonl.
            event(repo, "loop_error", error=repr(e)[:300])
            try:
                (d / "submit.log").write_text(f"{utc_now()} {e!r}\n")
            except OSError:
                pass                # the event above already records it
            time.sleep(RETRY_EVERY)
            continue
        _nap(repo, INTERVAL, wake=not errors.get("request_stuck"))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "_loop":
        _loop(pathlib.Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "start")
