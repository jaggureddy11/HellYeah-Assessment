"""A pausable session's clock, from the kit's side (spec §5).

The gate keeps the clock: write-once pause and resume events, and a `clock` in
every answer (gate/worker.js). The kit keeps a copy, .interview/ledger.json, of
where the session stands as the server last said, and the recorder rings at its
bell. Every write goes through apply(), under .interview/ledger.lock, and only
ever moves the copy forward (running n < paused n < running n+1): a late answer
never undoes a newer one (platform/formal/pause, MODE lastwrite). The bell is
always the server's, never one computed from when an answer arrived (MODE
localclock).

Standard library only: this ships in the kit.
"""
from __future__ import annotations
import contextlib, datetime, hashlib, json, math, os, pathlib, shutil, time

FILE = "ledger.json"
SECRET = "gate.json"
LOCK = "ledger.lock"
LOCKDIR = "ledger.lock.d"
OWNER = "owner"             # inside the lock directory: the holder's nonce
STALE_LOCKDIR = 30.0        # holders keep the lock for milliseconds; older is a holder that died
WAIT_LOCKDIR = 45.0         # longer than STALE_LOCKDIR, so a dead holder's lock is broken before we give up
POLL_LOCKDIR = 0.05


def seq(st: dict) -> int:
    """Where a state sits in the session's order: running 1, paused 1, running 2, ..."""
    if not st:
        return 0
    return 2 * int(st["n"]) - (1 if st["phase"] == "running" else 0)


def from_clock(clock: dict) -> dict:
    """The local state an answer's clock describes. ValueError if it is not a
    clock, or if its `now` is not a finite number: the kit times its copy by it
    (as_of), and the gate never writes one that is not (gate._measure)."""
    try:
        if clock["phase"] not in ("running", "paused"):
            raise ValueError(f"phase {clock['phase']!r}")
        if not math.isfinite(float(clock["now"])):
            raise ValueError(f"now {clock['now']!r}")
        return {"phase": clock["phase"], "n": int(clock["n"]), "bell": float(clock["bell"]),
                "window_end": float(clock["window_end"]), "budget": float(clock["budget"]),
                "opened_at": float(clock["opened_at"]), "as_of": float(clock["now"]),
                "events": [{"kind": str(e["kind"]), "n": int(e["n"]), "at": float(e["at"])}
                           for e in clock["events"]]}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"not a clock: {e}") from None


@contextlib.contextmanager
def _locked(repo: pathlib.Path):
    """The ledger's lock: flock where the filesystem has it, else the lock
    directory .interview/ledger.lock.d. Blocking: every holder is quick.

    Not re-entrant: a second acquire in the same process waits for the first.
    Under flock (locks are per open file) it waits for ever; with the lock
    directory it waits until it breaks its own lock as stale, and both are in.
    Nothing called while it is held may take it again: inside, use _read,
    never load or apply.

    The lock directory is model-checked in platform/formal/ledgerlock, where
    the simpler ways of breaking a dead holder's lock let two holders in."""
    iv = repo / ".interview"
    iv.mkdir(parents=True, exist_ok=True)
    f = None
    try:
        import fcntl
        f = open(iv / LOCK, "a+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        if f is not None:
            f.close()
        f = None
    if f is not None:
        try:
            yield
        finally:
            f.close()
        return
    d = iv / LOCKDIR
    me = f"{os.getpid()}-{os.urandom(8).hex()}"
    mine = iv / f"{LOCK}.{me}.tmp"          # *.tmp in .interview/: never snapshotted
    os.mkdir(mine)
    try:
        (mine / OWNER).write_text(me)
        end = time.time() + WAIT_LOCKDIR
        while True:
            os.utime(mine)                  # the lock's age counts from here: rename keeps an mtime
            try:
                os.rename(mine, d)          # taken: it arrives with its owner, so it is never empty
                break
            except OSError:
                pass                        # held: renaming onto a non-empty directory fails
            _break_if_stale(iv, d, me)
            if time.time() > end:
                raise TimeoutError(f"{d} is held. If no interview command is running, remove it.")
            time.sleep(POLL_LOCKDIR)
    except BaseException:
        shutil.rmtree(mine, ignore_errors=True)
        raise
    try:
        yield
    finally:
        _release(iv, d, me)


def _owner(d: pathlib.Path) -> str | None:
    """The nonce of the lock directory at d: None if there is none there, ""
    if one is there without an owner (left by something else, never a live lock)."""
    try:
        return (d / OWNER).read_text()
    except FileNotFoundError:
        return "" if d.is_dir() else None
    except OSError:
        return None


def _break_if_stale(iv: pathlib.Path, d: pathlib.Path, me: str) -> None:
    """Removes the lock directory at d if its holder died. Several waiters can
    find it stale at once, and one of them may act only after another has
    already broken it and a third has taken the lock. So a breaker first makes
    d/claim-<nonce> (mkdir: one claimer per lock), then checks that d still has
    the nonce it judged: if so the claim is in the dead lock, which nobody else
    can now break, and it is renamed away and removed; if not, the claim went
    into a newer lock and is taken out again."""
    n0 = _owner(d)
    if n0 is None:
        return
    try:
        if time.time() - d.stat().st_mtime <= STALE_LOCKDIR:
            return
    except OSError:
        return
    claim = d / ("claim-" + hashlib.sha256(n0.encode()).hexdigest()[:16])
    try:
        os.mkdir(claim)
    except OSError:
        return                              # another waiter is breaking it, or it has gone
    if _owner(d) != n0:
        try:
            os.rmdir(claim)
        except OSError:
            pass                            # gone with the lock it was in
        return
    dead = iv / f"{LOCK}.{me}.dead.tmp"
    try:
        os.rename(d, dead)
    except OSError:
        return
    shutil.rmtree(dead, ignore_errors=True)


def _release(iv: pathlib.Path, d: pathlib.Path, me: str) -> None:
    """Removes the lock directory only if it is still ours: one held past the
    stale age may have been broken and taken by another. Renamed away whole,
    so that a stray claim inside cannot keep it in place."""
    if _owner(d) != me:
        return
    gone = iv / f"{LOCK}.{me}.tmp"
    try:
        os.rename(d, gone)
    except OSError:
        return
    shutil.rmtree(gone, ignore_errors=True)


def _parse(f: pathlib.Path) -> dict | None:
    """The state stored in f, if it is exactly one from_clock makes, else None."""
    try:
        st = json.loads(f.read_text())
        exact = type(st["n"]) is int and st["n"] >= 1 and from_clock({**st, "now": st["as_of"]}) == st
    except (OSError, KeyError, TypeError, ValueError):
        return None                     # missing, unreadable, or not shaped as a state: none is stored there
    return st if exact else None


def _write(repo: pathlib.Path, st: dict) -> None:
    from . import record
    f = repo / ".interview" / FILE
    tmp = f.with_name(FILE + ".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, f)
    record.mirror(repo, FILE)


def _read(repo: pathlib.Path) -> dict:
    """The local state; call it under the lock. Deleted (git clean -x), or there
    but unreadable: the mirror under .git puts it back."""
    from . import record
    record.restore(repo, FILE)
    f = repo / ".interview" / FILE
    st = _parse(f)
    if st is None and f.exists():
        m = record.mirror_dir(repo)
        st = _parse(m / FILE) if m is not None else None
        if st is not None:
            _write(repo, st)
            record.event(repo, "restored", file=FILE, detail="unreadable in .interview/, put back from .git/interview/")
    return st or {}


def _settle(repo: pathlib.Path, st: dict) -> None:
    """Brings what follows the ledger up to it: the mirror, and recorder.json's
    deadline, which is the bell. Done on every answer, changed or not, so that a
    write lost between the files (a crash, an OSError, the recorder writing an
    old deadline) is put right by the next one."""
    from . import record
    m = record.mirror_dir(repo)
    if m is not None:
        try:
            behind = (m / FILE).read_bytes() != (repo / ".interview" / FILE).read_bytes()
        except OSError:
            behind = True
        if behind:
            record.mirror(repo, FILE)
    rec = record.state(repo)
    if rec.get("deadline") and not rec.get("selftest") and rec["deadline"] != st["bell"]:
        rec["deadline"] = st["bell"]
        record._write_state(repo, rec)


def load(repo: pathlib.Path) -> dict:
    with _locked(pathlib.Path(repo)):
        return _read(pathlib.Path(repo))


def apply(repo: pathlib.Path, clock: dict, measured: tuple[float, float] | None = None) -> tuple[dict, bool]:
    """Moves the local copy to what `clock` says, if that is further along, and
    points recorder.json's deadline at the bell of the state held. Returns that
    state and whether it changed.

    `measured` is the offset the answer carrying `clock` measured, and its
    error (gate._measure). It is kept first (record.note_gate_offset), before
    recorder.json's deadline can move: the recorder times the bell with the
    offset in force, and one kept from an earlier sitting, before the machine's
    clock was corrected, would ring the new bell early or late by the
    correction. An answer older than the copy still measures the offset of
    now; record.note_gate_offset keeps whichever measurement is the better."""
    from . import record
    repo = pathlib.Path(repo)
    new = from_clock(clock)
    with _locked(repo):
        if measured is not None:
            try:
                record.note_gate_offset(repo, *measured)
            except Exception as e:
                # The offset in force stays; the clock is applied all the same,
                # since a bell held back by a note that failed would ring late.
                record.event(repo, "clock_note_error", error=repr(e)[:300])
        cur = _read(repo)
        changed = seq(new) > seq(cur)
        if changed:
            _write(repo, new)
            cur = new
        elif seq(new) < seq(cur):
            record.event(repo, "clock_stale", have=seq(cur), got=seq(new))
        if cur:
            _settle(repo, cur)
    return cur, changed


def settle(repo: pathlib.Path) -> dict:
    """Brings recorder.json's deadline to the bell of the state held, with no
    answer to apply: the recorder's sync when the gate did not answer. The
    ledger is the kit's copy of the server's clock and recorder.json follows it
    (spec §5.1), so a write lost between the two (an OSError or a kill inside
    apply, or recorder.json put back a tick stale) is put right without the
    server, before the bell decides (spec §5.4). Returns the state held."""
    repo = pathlib.Path(repo)
    with _locked(repo):
        cur = _read(repo)
        if cur:
            _settle(repo, cur)
    return cur


def active_seconds(st: dict, now: float) -> float:
    """Active time so far, on the server's clock: what checkpoints fall due by."""
    start, used = st["opened_at"], 0.0
    for e in st["events"]:
        if e["kind"] == "pause":
            used += e["at"] - start
        else:
            start = e["at"]
    if st["phase"] == "running":
        used += max(0.0, min(now, st["bell"]) - start)
    return used


def left(st: dict, now: float) -> float:
    return st["budget"] - active_seconds(st, now)


def used(st: dict, now: float) -> str:
    """The first of the three things `pause` prints (spec §5.3): the time used.
    describe() gives the other two, the time left and the last moment to resume."""
    return f"You have used {_hm(active_seconds(st, now))} of your {_hm(st['budget'])}"


def _hm(sec: float) -> str:
    sec = max(int(sec), 0)
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def _local(t: float, offset: float) -> str:
    """A server time on this machine's clock, as the candidate reads it."""
    return datetime.datetime.fromtimestamp(t - offset).strftime("%a %d %b %H:%M")


def describe(st: dict, now: float, offset: float = 0.0) -> str:
    """For `status` and the agent's time_left. `now` is on the server's clock."""
    if st["phase"] == "running":
        return (f"running, {_hm(st['bell'] - now)} left; your clock stops at {_local(st['bell'], offset)} "
                f"your time unless you pause it (./interview pause)")
    rest = left(st, now)
    since = max((e["at"] for e in st["events"] if e["kind"] == "pause"), default=now)
    by = st["window_end"] - rest
    when = (f"Resume by {_local(by, offset)} to have all of it" if by > now else
            f"Resume now: the window ends {_local(st['window_end'], offset)}, so only "
            f"{_hm(st['window_end'] - now)} of it can still be used")
    return (f"paused since {_local(since, offset)}, {_hm(rest)} left. {when} (./interview resume). "
            f"Do not work on the exercise while paused")


def session(repo: pathlib.Path) -> dict:
    """session.json when this kit's clock can be paused, else {}."""
    try:
        s = json.loads((pathlib.Path(repo) / ".interview" / "session.json").read_text())
    except (OSError, ValueError):
        return {}
    return s if isinstance(s, dict) and s.get("pausable") and s.get("gate") and s.get("id") else {}


def keep_passphrase(repo: pathlib.Path, passphrase: str) -> None:
    """So that `pause` and `resume` take no argument. Mode 600 before the secret
    is in it, even when the file was already there; never through a symlink.
    Never snapshotted (record.EXCLUDES) nor packaged (submit.files)."""
    f = pathlib.Path(repo) / ".interview" / SECRET
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as out:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)        # open() leaves an existing file's mode as it was
        else:
            os.chmod(f, 0o600)
        json.dump({"passphrase": passphrase.strip()}, out)


def passphrase(repo: pathlib.Path) -> str | None:
    try:
        kept = json.loads((pathlib.Path(repo) / ".interview" / SECRET).read_text())
    except (OSError, ValueError):
        return None
    v = kept.get("passphrase") if isinstance(kept, dict) else None
    return v if isinstance(v, str) and v else None


def sync(repo: pathlib.Path, timeout: float = 5.0) -> tuple[dict, str | None]:
    """Asks the gate where the session stands (one try) and applies the answer.
    Returns the local state and, if the phase changed, the new phase. For a
    caller that acts on the server's answer whatever it is (`pause` asks with
    `pause n` either way); one that tells the candidate the state uses
    sync_heard."""
    st, moved, _ = sync_heard(repo, timeout)
    return st, moved


def sync_heard(repo: pathlib.Path, timeout: float = 5.0) -> tuple[dict, str | None, bool]:
    """sync, and a third item: whether the server answered with a clock this
    kit could read. Without an answer the state is only the kit's copy, and
    nothing may state it as the server's (spec §5.2: the candidate is never
    told something the server has not said)."""
    from . import gate
    repo = pathlib.Path(repo)
    sess = session(repo)
    if not sess:
        return {}, None, False
    before = load(repo)
    t0 = time.time()
    c = gate.session_clock(sess["gate"], sess["id"], timeout)
    t1 = time.time()
    st, moved, heard = before, None, False
    if c is not None:
        try:
            # One try, so the server read its clock between t0 and t1 (apply).
            st, changed = apply(repo, c, gate._measure(c["now"], t0, t1))
            moved = st["phase"] if changed and st.get("phase") != before.get("phase") else None
            heard = True
        except (KeyError, TypeError, ValueError):
            pass                        # a clock this kit cannot read says nothing it can use
    return st, moved, heard


def snapshot(repo: pathlib.Path, wait: float = 30.0) -> str:
    """A snapshot of the work as it is now, and its commit: what a pause or a
    resume names (spec §5.3), so verify can tell what changed in between. Only
    the session lock's holder snapshots (record.py). With a recorder running,
    ask it; with none, take the lock and snapshot here."""
    import secrets
    from . import record
    repo = pathlib.Path(repo)
    iv = repo / ".interview"
    held = record.try_lock(repo)
    if held is not None and not (isinstance(held, record._NoLock) and record.alive(repo)):
        try:
            record._snapshot(repo, {})
        finally:
            held.close()
        return record.head(repo)
    token = secrets.token_hex(4)
    # Written whole, then renamed into place: the recorder acts the moment the
    # request exists (record._nap), and a request it read half written would be
    # answered with a token nobody waits for, and deleted, leaving this command
    # to wait out its whole `wait`. *.tmp in .interview/ is never snapshotted.
    tmp = iv / f"snapshot-requested.{token}.tmp"
    tmp.write_text(token)
    os.replace(tmp, iv / "snapshot-requested")
    end = time.time() + wait
    while time.time() < end:
        try:
            done = json.loads((iv / "snapshot-done.json").read_text())
            if done.get("token") == token:
                return str(done.get("head") or "")
        except (OSError, ValueError):
            pass                        # not answered yet
        time.sleep(0.2)
    return record.head(repo)            # no answer in time: the latest snapshot is the best there is
