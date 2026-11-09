"""Checkpoints: a copy of the recording, uploaded every 30 minutes.

Each checkpoint goes to its own single-use upload link. The storage server
stamps it with its own clock when it arrives, which makes the record hard to
rewrite afterwards: every snapshot and every chat message a checkpoint holds
must still be in the final submission. Losing the laptop halfway also loses
nothing that was already checkpointed.

What a checkpoint holds, and nothing else:
  snapshots.git          the working-tree snapshots so far
  agent_logs/            the chat history collected so far, same scope as at the end
  transcript.jsonl, questions.jsonl, tool_calls.jsonl, recorder_events.jsonl,
  session_links.jsonl
  environment.json       runtime and package versions (see environment())
  checkpoint.json        when it was built, the snapshot head, a hash of every file

The recorder calls tick() once per loop. Building and uploading run in a
child process, so a slow uplink never stops the snapshots, and the bell
abandons an upload still in flight rather than wait for it. A checkpoint that
fails to upload (offline, say) is tried again on later ticks; a late one is
still sent, and its arrival time says when. The panel's side checks them with
`interview verify`.
"""
from __future__ import annotations
import datetime, json, os, pathlib, platform, shutil, subprocess, sys
import tarfile, tempfile, time

LOG = "checkpoints.json"
DONE = ("ok", "exists")
APPEND_ONLY = ("transcript.jsonl", "questions.jsonl", "tool_calls.jsonl", "recorder_events.jsonl",
               "session_links.jsonl")
PUT_TIMEOUT = 180


PID = "checkpoint.pid"
_child = None          # the upload process this recorder started, so it can be reaped


def _quiet(repo: pathlib.Path | None = None) -> float:
    """Seconds before the deadline when no checkpoint starts: the final
    submission is about to carry everything. The panel seals it into the kit
    with the schedule (links --quiet); otherwise two minutes at the real tick."""
    from . import record, submit
    q = submit.upload_doc(repo).get("quiet_minutes") if repo is not None else None
    return float(q) * 60 if q is not None else record.INTERVAL * 6


def plan(repo: pathlib.Path) -> list[dict]:
    from . import submit
    return list(submit.upload_doc(repo).get("checkpoints") or [])


def _log_path(repo: pathlib.Path) -> pathlib.Path:
    return repo / ".interview" / LOG


def load_log(repo: pathlib.Path) -> dict:
    try:
        return json.loads(_log_path(repo).read_text())
    except Exception:
        return {}


def _save_log(repo: pathlib.Path, log: dict) -> None:
    from . import submit
    submit._write_json(_log_path(repo), log)


# Why due() last deferred a pausable session's checkpoints. due() runs on every
# tick, and a ledger that cannot be read would otherwise be logged on every one:
# each call reads the cause the call before it kept here, and logs only a new one.
_deferral = {"why": None}


def due(repo: pathlib.Path, st: dict, now: float | None = None) -> list[dict]:
    from . import pause, record
    # A pausable kit runs on the server's clock (recorder.json's "clock" is
    # "server"): record.now is the gate's and R2's clock, the one the ledger's
    # times are written in, so active_seconds takes it as it is.
    now = record.now(repo, st) if now is None else now
    log = load_log(repo)
    if pause.session(repo):
        # A pausable session's checkpoints fall due by active time, by the kit's
        # ledger: none while paused (spec §5.4). With no ledger to count by, none
        # falls due until there is one again. The links are single use and _send
        # spends every pending one on one archive, so a count by wall time could
        # spend them all in one tick of a pause, for good; a checkpoint deferred
        # still goes later, with everything since. A ledger not written yet (start
        # sets it after the recorder's first tick, or its first clock could not
        # be read) is expected, and not logged; one there but unreadable is.
        try:
            led = pause.load(repo)
            why = None if led or not (repo / ".interview" / pause.FILE).exists() else "not a clock"
        except Exception as e:
            led, why = {}, repr(e)[:300]
        if why != _deferral["why"]:
            _deferral["why"] = why
            if why:
                record.event(repo, "checkpoint_deferred", detail="the ledger cannot be read", error=why)
        if not led:
            return []
        active = pause.active_seconds(led, now)
        return [c for c in plan(repo) if float(c["offset_minutes"]) * 60 <= active
                and (log.get(str(c["n"])) or {}).get("status") not in DONE]
    base = st.get("checkpoint_base") or st.get("started")
    if not base:
        return []
    return [c for c in plan(repo)
            if base + float(c["offset_minutes"]) * 60 <= now
            and (log.get(str(c["n"])) or {}).get("status") not in DONE]


def summary(repo: pathlib.Path) -> str:
    p = plan(repo)
    if not p:
        return ""
    log = load_log(repo)
    sent = sum(1 for c in p if (log.get(str(c["n"])) or {}).get("status") in DONE)
    return f"{sent}/{len(p)} checkpoints uploaded"


def tick(repo: pathlib.Path, st: dict) -> None:
    """Uploads whatever is due. Never raises: a checkpoint must not stop the recorder."""
    from . import record
    try:
        _send_started(repo)
        if record.time_left(repo, st) < _quiet(repo) or _running(repo):
            return
        if not due(repo, st):
            return
        if time.time() - float(load_log(repo).get("_last_attempt", 0)) < record.RETRY_EVERY:
            return
        _spawn(repo)
    except Exception as e:
        record.event(repo, "checkpoint_error", error=repr(e)[:300])


def _running(repo: pathlib.Path) -> int | None:
    """The pid of a checkpoint upload still in progress, if any."""
    global _child
    if _child is not None:
        if _child.poll() is None:            # also reaps it once it has exited
            return _child.pid
        _child = None
        return None
    # Started by an earlier recorder: the child holds checkpoint.lock while it
    # runs, which a reused pid cannot fake.
    probe = _child_lock(repo)
    if probe is None:
        try:
            return int((repo / ".interview" / PID).read_text().strip())
        except (OSError, ValueError):
            return -1
    if probe == "unsupported":                  # no locks here: fall back to the pid
        try:
            pid = int((repo / ".interview" / PID).read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None
    probe.close()
    return None


def _child_lock(repo: pathlib.Path):
    """The upload's lock, held in both places like the recorder's (see
    record._lock_files): an object to close, None if another upload holds it,
    or "unsupported" on a filesystem that cannot lock."""
    from . import record
    d = record.dir_lock(repo / ".interview")      # not the repository: the recorder holds that one
    if d is None or d != "unsupported":
        return d
    places = ([record.mirror_dir(repo)] if record.mirror_dir(repo) else []) + [repo / ".interview"]
    got = []
    for d in places:
        f = record._flock(d / "checkpoint.lock")
        if f is None:
            for g in got:
                g.close()
            return None
        if f != "unsupported":
            got.append(f)
    return record._Held(got) if got else "unsupported"


def _spawn(repo: pathlib.Path) -> None:
    global _child
    pkg_parent = str(pathlib.Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    with open(repo / ".interview" / "recorder.log", "a") as log:
        _child = subprocess.Popen([sys.executable, "-m", "ivkit.checkpoint", "_send", str(repo)],
                                  stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env)
    (repo / ".interview" / PID).write_text(str(_child.pid))


def abandon(repo: pathlib.Path) -> None:
    """Stops an upload in flight. Called at the bell: the final carries
    everything a checkpoint would have."""
    import signal
    pid = _running(repo)
    if pid:
        try:
            if pid > 0:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass                            # it already finished
        for _ in range(20):
            if not _running(repo):
                break
            time.sleep(0.1)
        from . import record
        record.event(repo, "checkpoint_abandoned", detail="the bell rang during an upload")
    (repo / ".interview" / PID).unlink(missing_ok=True)


def _child_main(repo: pathlib.Path) -> None:
    import signal
    from . import record
    held = _child_lock(repo)
    if held is None:
        return                              # another upload is already running
    # the bell's SIGTERM still runs the finally blocks, so no copy of the
    # candidate's snapshots and chats is left in the temp directory
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(1))
    try:
        st = record.state(repo)
        pending = due(repo, st)
        if pending:
            _send(repo, st, pending)
    except Exception as e:
        record.event(repo, "checkpoint_error", error=repr(e)[:300])
    finally:
        # the back-off runs from the end of an attempt, so a slow link is not hammered
        log = load_log(repo)
        log["_last_attempt"] = time.time()
        _save_log(repo, log)
        try:
            if int((repo / ".interview" / PID).read_text()) == os.getpid():
                (repo / ".interview" / PID).unlink()
        except (OSError, ValueError):
            pass                            # no pid file, or another upload's: not ours to remove
        if held != "unsupported":
            held.close()                    # held to here: the lock covers the log and the pid file too


def _send_started(repo: pathlib.Path) -> None:
    """The start marker: a tiny write-once upload at `interview start`, so the
    panel has a server-side time for when the session really began. Tried
    again on later ticks if it did not get through at start."""
    from . import submit, record
    link = submit.upload_doc(repo).get("started")
    log = load_log(repo)
    prev = log.get("started") or {}
    if not link or prev.get("status") in DONE:
        return
    if time.time() - float(prev.get("at", 0)) < record.RETRY_EVERY:
        return
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="interview-started-"))
    try:
        body = tmp / "started.json"
        st = record.state(repo)
        body.write_text(json.dumps({"started_local": st.get("started"), "clock": st.get("clock"),
                                    "clock_offset": st.get("clock_offset"),
                                    "offset_measured": st.get("offset_measured")}))
        status, code = submit.put(link["url"], body, timeout=30, repo=repo)
        log = load_log(repo)
        log["started"] = {"status": status, "http": code, "attempted_at": record.utc_now(),
                          "at": time.time()}
        _save_log(repo, log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _send(repo: pathlib.Path, st: dict, pending: list[dict]) -> None:
    from . import submit, record
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="interview-checkpoint-"))
    try:
        path, meta = build(repo, [c["n"] for c in pending], tmp)
        digest = submit.sha256(path)
        for c in pending:
            if record.time_left(repo, st) < _quiet(repo):
                break                       # the bell comes first; the final carries it
            status, code = submit.put(c["url"], path, timeout=PUT_TIMEOUT, repo=repo)
            log = load_log(repo)
            prev = log.get(str(c["n"])) or {}
            log[str(c["n"])] = {"status": status, "http": code, "sha256": digest,
                                "bytes": path.stat().st_size, "built_at": meta["built_at"],
                                "snapshots_head": meta["snapshots_head"],
                                "attempted_at": record.utc_now(),
                                "attempts": prev.get("attempts", 0) + 1}
            _save_log(repo, log)
            record.event(repo, "checkpoint", n=c["n"], status=status, http=code,
                         **({"error": submit.last_error} if submit.last_error else {}))
            if status not in DONE:
                break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build(repo: pathlib.Path, ns: list[int], tmp: pathlib.Path) -> tuple[pathlib.Path, dict]:
    from . import collect, submit
    iv = repo / ".interview"
    stage = tmp / "checkpoint"
    stage.mkdir()
    snap = iv / "snapshots.git"
    if (snap / "HEAD").exists():
        _copy_snapshots(snap, stage / "snapshots.git")
    collection = {}
    try:
        collection = collect.run(repo, out=stage / "agent_logs", quiet=True).get("collection", {})
    except Exception as e:
        (stage / "collect_error.txt").write_text(repr(e))
    for name in APPEND_ONLY + (LOG,):
        if (iv / name).exists():
            shutil.copy2(iv / name, stage / name)
    (stage / "environment.json").write_text(json.dumps(environment(repo), indent=1))
    # the head of the COPY, not of the live repository. The recorder commits
    # every 20 s and this runs beside it, so reading the live head describes a
    # history this checkpoint does not contain --- and `verify`, comparing the
    # two, reports an honest candidate for holding a snapshot it never sent.
    copied = stage / "snapshots.git"
    head = subprocess.run(["git", f"--git-dir={copied}", "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60).stdout.strip() \
        if (copied / "HEAD").exists() else ""
    meta = {"checkpoints": ns, "built_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"), "snapshots_head": head or None, "collection": collection,
            "files": {str(p.relative_to(stage)): submit.sha256(p)
                      for p in sorted(stage.rglob("*")) if p.is_file()}}
    (stage / "checkpoint.json").write_text(json.dumps(meta, indent=1))
    out = tmp / "checkpoint.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(stage, arcname="checkpoint")
    return out, meta


def _copy_snapshots(src: pathlib.Path, dst: pathlib.Path) -> None:
    """The recorder may commit while this copies. Refs first, objects after:
    objects are never changed once written, so every ref copied points at an
    object that is copied too."""
    dst.mkdir(parents=True)
    for name in ("HEAD", "packed-refs", "config"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    if (src / "refs").exists():
        shutil.copytree(src / "refs", dst / "refs")
    if (src / "objects").exists():
        shutil.copytree(src / "objects", dst / "objects", ignore=shutil.ignore_patterns("tmp_*"))


def _version(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip() or None
    except Exception:
        return None


def environment(repo: pathlib.Path) -> dict:
    """What reproducing the candidate's numbers needs: runtime versions, and the
    packages installed in any virtual environment at the top of the repository.
    Lockfiles and requirements files are in the repository itself."""
    env: dict = {"recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                 "platform": platform.platform(), "kit_python": sys.version.split()[0], "tools": {}}
    for name in ("python3", "uv", "node", "npm", "git"):
        if shutil.which(name):
            env["tools"][name] = _version([name, "--version"])
    venvs = {}
    for cfg in sorted(repo.glob("*/pyvenv.cfg")):
        v = cfg.parent
        py = v / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        if not py.exists():
            continue
        freeze = _version([str(py), "-m", "pip", "freeze"])
        if (not freeze or "No module named pip" in freeze) and shutil.which("uv"):
            freeze = _version(["uv", "pip", "freeze", "--python", str(py)])
        venvs[str(v.relative_to(repo))] = {"python": _version([str(py), "--version"]),
                                           "freeze": (freeze or "").splitlines()}
    env["venvs"] = venvs
    return env


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "_send":
        _child_main(pathlib.Path(sys.argv[2]))
