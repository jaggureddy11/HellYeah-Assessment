#!/usr/bin/env python3
"""Freeze, package and upload the session.

The recorder does this by itself when the time is up. You can also run it
yourself to submit early:

    ./interview submit

It commits anything you left uncommitted, gathers your agent chat history,
writes one archive, and uploads it. The upload is a single-use link that only
accepts this one file, so nothing of yours is exposed and you need no
credentials.

A session is frozen once. If the upload fails, the archive stays on disk and
the recorder keeps retrying that same archive until the link expires; running
`submit` again makes it retry at once. It is packaged again only if that
archive has vanished from disk, and the manifest then says so. Nothing is
lost.

Only one process ever packages: whoever holds the session lock (see
record.py). When the recorder is running, `submit` asks it to do the work and
waits for the result.

What goes in:
  your repository, with its git history as .interview/history.bundle
  .interview/snapshots.git   the 20-second working-tree snapshots
  .interview/agent_logs/     chat history from whatever tools you used
  .interview/questions.jsonl what you asked the panel
  .interview/session_links.jsonl
                             the links you recorded with ./interview link
  MANIFEST.json              file hashes, both git heads, timestamps, and the
                             versions of your runtimes and packages
Virtual environments, node_modules and caches are left out; their package
lists are in the manifest instead.
"""
from __future__ import annotations
import json, hashlib, os, secrets, shutil, subprocess, tarfile, datetime, pathlib, tempfile, time

from . import collect as collect_mod
from . import record as record_mod

ROOT = pathlib.Path(".").resolve()
HERE = ROOT / ".interview"
# Every directory the recorder never snapshots (record.EXCLUDES) is also left
# out of the archive. A directory that ships but is in no snapshot is a place
# to keep work the record never saw, and `target/` and `dist/` were exactly
# that until this line named them too.
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
# Build directories the recorder never snapshots either, so shipping them would
# be the only copy of work nothing recorded. At the repository root only: a
# `docs/dist/` is the candidate's writing, not a build.
ROOT_SKIP = {"target", "dist"}
# Kept on the candidate's machine only: a pausable kit's passphrase (pause.py).
PRIVATE = {".interview/gate.json"}
LEFT_OUT: list[str] = []                      # what the last `files()` walk dropped

DONE = ("ok", "already-submitted")           # R2 holds the submission
GIVEN_UP = ("none", "bad-config", "expired")  # nothing more an upload attempt can do
WAIT_FOR_RECORDER = 900                       # seconds `submit` waits on the recorder
last_error = ""                               # why the latest put() failed, for the record


def _sh(*args, cwd=None):
    try:
        r = subprocess.run(args, cwd=cwd or ROOT, capture_output=True, text=True, timeout=120)
        return r.stdout.strip()
    except Exception:
        return ""


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


BUNDLE_ERROR: list[str] = []          # why the history did not travel, if it did not


def _bundle_history() -> None:
    """The candidate's own commits, as a bundle next to the session's files.

    `.git` itself is not packaged: a git directory is arbitrary file names by
    design, so nothing can separate what git wrote from what anyone else put
    there, and the recorder snapshots none of it. A bundle carries every ref
    and every object reachable from one, and nothing that is not an object --
    the same history, with no room beside it. Best effort: a repository with no
    commits has nothing to bundle, and the submission is not worth failing over
    a history the panel can also read from the snapshots."""
    dst = HERE / "history.bundle"
    BUNDLE_ERROR.clear()
    try:
        dst.unlink(missing_ok=True)
    except OSError:
        shutil.rmtree(dst, ignore_errors=True)   # a directory by that name is not ours to keep
        if dst.exists():
            BUNDLE_ERROR.append(f"{dst.name} could not be replaced")
            return
    if not (ROOT / ".git").exists():
        BUNDLE_ERROR.append("there is no git repository here, so there are no commits to send")
        return
    lock = pathlib.Path(str(dst) + ".lock")       # git's, from a run that died
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        shutil.rmtree(lock, ignore_errors=True)
        if lock.exists():
            BUNDLE_ERROR.append(f"{lock.name} is in the way and could not be removed")
            return
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "bundle", "create", str(dst), "--all"],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:                      # nothing here is worth losing a submission over
        dst.unlink(missing_ok=True)
        BUNDLE_ERROR.append(type(e).__name__)
        return
    if r.returncode != 0 or not dst.exists():
        dst.unlink(missing_ok=True)
        why = (r.stderr or "").strip().splitlines()
        BUNDLE_ERROR.append(why[-1][:160] if why else "git failed")
        return
    # read back what was written. `git bundle create` returns 0 for a history it
    # cannot bundle usefully --- a shallow clone is the known one --- and opening
    # it is the only thing that says so. Better here, where the candidate can be
    # told, than on the panel's side where it looks like interference.
    # into a fresh temporary directory outside the repository, as the panel's
    # own readers do: a target inside .interview could already exist, would
    # survive an ignore_errors rmtree if it were a file, and would then be
    # packaged. And a clone that could not be attempted is not evidence about
    # the bundle: "I could not look" and "the bundle is bad" are different
    # answers, and only the second is worth deleting a candidate's history for.
    area = None
    try:
        area = pathlib.Path(tempfile.mkdtemp(prefix="vi-bundle-check-"))
        back = subprocess.run(["git", "clone", "-q", "--mirror", str(dst), str(area / "h.git")],
                              capture_output=True, text=True, timeout=600)
    except Exception as e:
        BUNDLE_ERROR.append(f"the bundle was written but could not be checked ({type(e).__name__}); "
                            f"it was kept")
        return
    finally:
        if area is not None:
            shutil.rmtree(area, ignore_errors=True)
    if back.returncode != 0:
        dst.unlink(missing_ok=True)
        why = (back.stderr or "").strip().splitlines()
        BUNDLE_ERROR.append("the bundle git wrote could not be opened again: " +
                            (why[-1][:140] if why else "git gave no reason"))


def files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every file to package, sorted. Never raises: a directory that vanishes
    or cannot be read mid-walk is skipped. Virtual environments (any directory
    holding pyvenv.cfg) are left out like .venv."""
    out, dropped = [], []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        here = pathlib.Path(dirpath)
        # SKIP_DIRS anywhere (caches and virtual environments are caches wherever
        # they are); ROOT_SKIP only at the top (`docs/dist/` is the candidate's
        # writing, `dist/` next to the policy is build output); and no .git at
        # all. A git directory is arbitrary file names by design --- an object
        # is 38 hex characters, a ref and a hook are whatever you call them ---
        # so nothing can tell a file git put there from a file somebody else
        # did, and the recorder snapshots none of it. The history still travels,
        # as a bundle: `history.bundle` carries every ref and object and nothing
        # that is not one.
        cut = sorted(n for n in dirnames
                     if n in SKIP_DIRS or (n in ROOT_SKIP and here == root)
                     or n == ".git"
                     or os.path.exists(os.path.join(dirpath, n, "pyvenv.cfg")))
        # .git is not "left out": the history travels as history.bundle, and
        # saying a directory every repository has was dropped would bury
        # the one line that matters -- the build directory that held work
        dropped += [str((here / n).relative_to(root)) for n in cut if n != ".git"]
        dirnames[:] = sorted(n for n in dirnames if n not in cut)
        for n in filenames:
            if str((here / n).relative_to(root)) in PRIVATE:
                continue
            if n.startswith("submission-") and (".tar.gz" in n):
                continue
            if n == ".git":
                # `git submodule add` and `git worktree add` write .git as a file
                # holding `gitdir: ...`; the directory walk above never sees it.
                # Not recorded as left out: every repository has one, and telling
                # the candidate to move it would be advice to break their tree.
                continue
            out.append(here / n)
    LEFT_OUT[:] = dropped                     # recorded in MANIFEST.json, never silent
    return sorted(out)


def _configure(repo) -> None:
    global ROOT, HERE
    if repo is not None:
        ROOT = pathlib.Path(repo).resolve(); HERE = ROOT / ".interview"


def _write_json(path: pathlib.Path, doc: dict) -> None:
    """Readers poll these files; they must never see half of one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)


def receipt(repo=None) -> dict:
    f = (pathlib.Path(repo).resolve() / ".interview" if repo is not None else HERE) / "submitted.json"
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def upload_doc(repo=None) -> dict:
    root = pathlib.Path(repo).resolve() if repo is not None else ROOT
    if not (root / ".interview" / "upload.json").exists() and record_mod.state(root).get("deadline"):
        record_mod.restore(root, "upload.json")       # a git clean -x must not lose the links
    for conf in (root / ".interview" / "upload.json", root / "upload.json"):
        if conf.exists():
            for _ in range(3):                   # a read that raced a write: look again
                try:
                    return json.loads(conf.read_text())
                except Exception:
                    time.sleep(0.2)
            m = record_mod.mirror_dir(root)
            if m is not None and (m / "upload.json").exists():
                try:
                    return json.loads((m / "upload.json").read_text())   # the second copy
                except Exception:
                    pass            # both copies unreadable: said so below
            return {"_unreadable": True}
    return {}


def expires_at(repo=None) -> float | None:
    """When the upload links stop working, server epoch seconds. Links never
    live beyond 7 days, so an unreadable expiry means 8 days after the start."""
    doc = upload_doc(repo)
    if not doc.get("url"):
        return None
    try:
        return record_mod.parse_iso(doc["expires_at"]).timestamp()
    except Exception:
        started = record_mod.state(pathlib.Path(repo or ROOT)).get("started")
        return started + 8 * 86400 if started else None


def expired(repo=None) -> bool:
    root = pathlib.Path(repo or ROOT)
    t = expires_at(repo)
    return t is not None and time.time() + record_mod.offset(root, record_mod.state(root)) > t


def _date_offset(date: str | None, t0: float, t1: float) -> float | None:
    import email.utils
    try:
        return email.utils.parsedate_to_datetime(date).timestamp() - (t0 + t1) / 2
    except Exception:
        return None


def server_offset(url: str) -> float | None:
    """Seconds to add to this machine's clock to get the storage server's.
    One HEAD against the upload link: the signature is for PUT, so the answer
    is 403, but every answer carries the server's Date. None when offline."""
    import urllib.request, urllib.error
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=5) as r:
            date = r.headers.get("Date")
    except urllib.error.HTTPError as e:
        date = e.headers.get("Date")
    except Exception:
        return None
    return _date_offset(date, t0, time.time())


def note_clock(repo: pathlib.Path, date: str | None, t0: float, t1: float) -> None:
    """Keeps the clock offset current from an upload reply's Date header.

    A pausable kit also measures it from every gate answer, more precisely:
    the Date header is whole seconds (1 s) and the server's time is somewhere
    within the request (half of t1 - t0). That measurement stays while this one
    agrees with it (record.keeps_gate_offset); one that does not means the
    machine's clock has moved, and this one is written. A kit that cannot pause
    has no gate measurement, and writes as it always has."""
    off = _date_offset(date, t0, t1)
    if off is not None and not record_mod.keeps_gate_offset(pathlib.Path(repo), off, (t1 - t0) / 2 + 1):
        try:
            _write_json(pathlib.Path(repo) / ".interview" / "clock.json", {"offset": off, "at": t1})
        except OSError:
            pass                    # the offset measured at start still stands


# ------------------------------------------------------------- entry points

def run(repo=None, holder=None) -> dict:
    """`holder` is the open session lock when the recorder calls this. From a
    terminal it is None: ask the recorder, or take the lock if nobody holds it."""
    _configure(repo)
    if holder is not None:
        return _execute()
    HERE.mkdir(parents=True, exist_ok=True)
    before = receipt()
    (HERE / "submit-requested").write_text(record_mod.utc_now() + "\n")
    lock = record_mod.try_lock(ROOT)
    if isinstance(lock, record_mod._NoLock) and record_mod.alive(ROOT):
        lock = None                          # cannot lock here: defer to the running recorder
    if lock is None:
        print("\n  asked the recorder to package and upload your session. Waiting for it.")
        return _wait_for_holder(before)
    try:
        r = _execute()
    except Exception as e:
        print(f"\n  packaging failed ({e!r}).")
        r = receipt() or {"upload": "error"}
    finally:
        lock.close()
    _hand_over(r)
    return r


def _hand_over(r: dict) -> None:
    """We held the lock, so no recorder was running. If the job is not done,
    start one to own the retries."""
    if r.get("upload") in DONE or r.get("upload") in GIVEN_UP:
        return
    st = record_mod.state(ROOT)
    if st.get("deadline") and not st.get("selftest"):
        record_mod._spawn_loop(ROOT, "submit")
        print("  The recorder has taken over and keeps retrying.\n")
    else:
        print("  Run ./interview submit again once you are back online.\n")


def retry(repo=None) -> dict:
    """Uploads the frozen archive again. The caller holds the lock."""
    _configure(repo)
    r = receipt()
    if not r.get("archive") or not pathlib.Path(r["archive"]).exists():
        return r
    return _upload_frozen(r, quiet=True)


def give_up(repo=None) -> None:
    _configure(repo)
    r = receipt()
    if r:
        on_disk = bool(r.get("archive")) and pathlib.Path(r["archive"]).exists()
        r["upload"] = "expired"
        r["detail"] = ("The upload link has expired. " +
                       (f"The archive is on disk at {r.get('archive')}, sha256 {r.get('sha256', '')[:16]}...; "
                        f"send it to the panel." if on_disk else
                        "The archive is no longer on disk either. Tell the panel."))
        _write_json(HERE / "submitted.json", r)


def _settled(r: dict, before: dict) -> bool:
    """The holder has answered this request: a result exists that is newer
    than what was there when we asked."""
    up = r.get("upload")
    if not up or up == "pending":
        return False
    if up in DONE or up in GIVEN_UP:
        return True
    return r.get("attempts", 0) > before.get("attempts", 0) or r.get("sha256") != before.get("sha256")


def _wait_for_holder(before: dict) -> dict:
    end = time.time() + WAIT_FOR_RECORDER
    while time.time() < end:
        r = receipt()
        if _settled(r, before):
            _report(r)
            return r
        lock = record_mod.try_lock(ROOT)
        if lock is not None and not (isinstance(lock, record_mod._NoLock) and record_mod.alive(ROOT)):
            try:                             # the recorder went away: do it here
                r = _execute()
            finally:
                lock.close()
            _hand_over(r)
            return r
        time.sleep(1)
    print("  still packaging. `./interview status` shows when it is done.")
    return receipt() or {"upload": "pending"}


def _report(r: dict) -> None:
    print(f"\n  archive        {r.get('archive')}")
    print(f"  sha256         {r.get('sha256')}")
    # The packaging usually happens in the recorder, whose stdout is a log file,
    # so anything printed there reaches nobody. This runs in the candidate's
    # terminal, and it is the only place they will read it.
    if r.get("bundle_error"):
        print(f"\n  YOUR GIT HISTORY WAS NOT SENT: {'; '.join(r['bundle_error'])}")
        print("  The snapshots still hold your work. Tell the panel.")
    if r.get("left_out"):
        print(f"\n  NOT PACKAGED, as build output: {', '.join(r['left_out'])}")
        print("  The recorder never snapshotted these either. If one of them holds work you "
              "want read,\n  move it out of that directory and run ./interview submit again.")
    print(f"\n  {r.get('detail')}\n")


def _execute() -> dict:
    r = receipt()
    if r.get("upload") in DONE:
        print("\n  already submitted.")
        _report(r)
        return r
    if r.get("archive") and pathlib.Path(r["archive"]).exists():
        print(f"\n  The session was frozen at {r.get('frozen_at')}. Uploading that archive again;"
              f" nothing is packaged twice.")
        (HERE / "submit-requested").unlink(missing_ok=True)
        return _upload_frozen(r)
    return _freeze_and_upload(previous=r or None)


class _Exact:
    """Reads exactly `size` bytes from an open file, hashing them. A file that
    shrinks or fails mid-read is padded with zeros to the size in its tar
    header and marked short, so the archive stays readable; a file that grows
    is cut at that size."""

    def __init__(self, f, size: int):
        self.f, self.left, self.short = f, size, False
        self.h = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        try:
            b = self.f.read(n) if not self.short else b""
        except OSError:
            b = b""
        if len(b) < n:
            self.short = True
            b += b"\0" * (n - len(b))
        self.left -= len(b)
        self.h.update(b)
        return b


def _add(tf: tarfile.TarFile, p: pathlib.Path, arcname: str) -> dict | None:
    """Adds one path. Only opening and stat-ing it may fail, before anything is
    written: then it is left out and None returned. Links are stored as links,
    never followed out of the repository. Errors writing the archive itself
    propagate: the archive is then abandoned and packaging tried again."""
    tf.inodes = {}                  # a hardlinked file is stored whole, not as a link to another member
    if p.is_symlink():
        try:
            ti = tf.gettarinfo(str(p), arcname)
        except OSError:
            return None
        tf.addfile(ti)
        return {"path": arcname, "bytes": 0, "link": ti.linkname}
    ti = None
    try:
        with open(p, "rb") as f:
            ti = tf.gettarinfo(arcname=arcname, fileobj=f)
            if not ti.isreg():
                tf.addfile(ti)
                return {"path": arcname, "bytes": 0}
            body = _Exact(f, ti.size)
            tf.addfile(ti, body)
    except OSError:
        if ti is not None:          # opened and stat-ed: the archive's own write failed
            raise
        return None
    entry = {"path": arcname, "bytes": ti.size, "sha256": body.h.hexdigest()}
    if body.short:
        entry["short"] = True
    return entry


def _freeze_and_upload(previous: dict | None = None) -> dict:
    print("\nFreezing the session.\n")
    if previous:
        print(f"  the archive frozen at {previous.get('frozen_at')} is gone from disk; freezing again")

    # 1. commit whatever is loose, so nothing is lost; one last snapshot, so the
    #    archive and the snapshot history end on the same tree
    if (ROOT / ".git").exists():
        dirty = _sh("git", "status", "--porcelain")
        if dirty:
            _sh("git", "add", "-A", "--ignore-errors")
            _sh("git", "-c", "user.name=panel", "-c", "user.email=panel@local",
                "commit", "-m", "submission: uncommitted work at the bell")
            print(f"  committed {len(dirty.splitlines())} uncommitted path(s) for you")
        else:
            print("  working tree was already clean")
    if (HERE / "snapshots.git" / "HEAD").exists():
        record_mod._snapshot(ROOT, {})

    # 2. collect agent history
    print("  collecting agent chat history")
    collection = {}
    try:
        res = collect_mod.run(ROOT)
        collection = res.get("collection", {})
        for line in res.get("found", []) + res.get("skipped", []):
            print("   " + line)
    except Exception as e:
        print(f"   collection failed: {type(e).__name__}")

    # 3. one pass: every file into the archive, hashed as it is written; the
    #    manifest goes in last, so it describes exactly what the archive holds.
    #    Written under a temporary name and read back before it counts.
    from . import checkpoint
    _bundle_history()
    name = f"submission-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.tar.gz"
    out = ROOT.parent / name
    part = out.with_name(name + ".part")
    top = pathlib.Path(ROOT.name)
    listed, skipped = [], []
    snap_git = HERE / "snapshots.git"
    try:
        with tarfile.open(part, "w:gz") as tf:
            for p in files(ROOT):
                if p == ROOT / "MANIFEST.json":
                    continue
                rel = str(p.relative_to(ROOT))
                entry = _add(tf, p, str(top / rel))
                if entry is None:
                    skipped.append(rel)
                else:
                    entry["path"] = rel
                    listed.append(entry)
            manifest = {
                "frozen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "repo_head": _sh("git", "rev-parse", "HEAD") or None,
                # --all, not HEAD: this is compared against what the bundle holds,
                # and the bundle carries every ref. A stash or a detached HEAD made
                # the two disagree on a session where nothing was wrong.
                "repo_commits": _sh("git", "rev-list", "--count", "--all") or None,
                "snapshots_head": _sh("git", f"--git-dir={snap_git}", "rev-parse", "HEAD") or None,
                "snapshots_count": _sh("git", f"--git-dir={snap_git}", "rev-list", "--count", "HEAD") or "0",
                "snapshots_first": _sh("git", f"--git-dir={snap_git}", "log", "--reverse",
                                       "--format=%aI", "-1") or None,
                "snapshots_last": _sh("git", f"--git-dir={snap_git}", "log", "--format=%aI", "-1") or None,
                "refrozen": previous is not None,
                "previous_freeze": {k: previous.get(k) for k in ("frozen_at", "sha256", "archive")}
                if previous else None,
                "environment": checkpoint.environment(ROOT),
                "collection": collection,
                "left_out": LEFT_OUT,      # directories not packaged; the panel sees them
                "bundle_error": list(BUNDLE_ERROR),   # why the history did not travel, if it did not
                "file_count": len(listed),
                "total_bytes": sum(e["bytes"] for e in listed),
                "files": listed,
                "skipped": skipped,
                "short": [e["path"] for e in listed if e.get("short")],
            }
            (ROOT / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
            tf.add(ROOT / "MANIFEST.json", arcname=str(top / "MANIFEST.json"))
        with tarfile.open(part) as check:          # a truncated or misaligned archive raises here
            for _ in check:
                pass
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    if LEFT_OUT:
        print(f"  not packaged, as build output: {', '.join(LEFT_OUT)}")
    if skipped:
        print(f"  left out {len(skipped)} unreadable file(s): "
              + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
    if manifest["short"]:
        print(f"  {len(manifest['short'])} file(s) changed while being packaged: "
              + ", ".join(manifest["short"][:5]))

    digest = sha256(out)
    size = out.stat().st_size
    print(f"\n  snapshots      {manifest['snapshots_count']}")
    print(f"  your commits   {manifest['repo_commits']}")
    print(f"  files          {len(listed)}")
    print(f"  archive        {out.name}  ({size/1e6:.1f} MB)")
    print(f"  sha256         {digest}")

    # 4. the freeze is on record before anything is sent: from here on, only this
    #    archive is ever uploaded
    r = {"archive": str(out), "sha256": digest, "bytes": size,
         "frozen_at": manifest["frozen_at"], "upload": "pending", "attempts": 0,
         "skipped": skipped, "left_out": list(LEFT_OUT), "bundle_error": list(BUNDLE_ERROR),
         "detail": "frozen, not uploaded yet"}
    _write_json(HERE / "submitted.json", r)
    (HERE / "submit-requested").unlink(missing_ok=True)
    return _upload_frozen(r)


def _upload_frozen(r: dict, quiet: bool = False) -> dict:
    path = pathlib.Path(r["archive"])
    status, detail = upload(path, r["sha256"], quiet=quiet)
    r = {**r, "upload": status, "detail": detail, "attempts": r.get("attempts", 0) + 1,
         "last_attempt": record_mod.utc_now()}
    _write_json(HERE / "submitted.json", r)
    if not quiet:
        print(f"\n  {detail}\n")
    return r


def put(url: str, path: pathlib.Path, timeout: float = 600,
        repo: pathlib.Path | None = None) -> tuple[str, int | None]:
    """One write-once PUT. Returns (status, http code): "ok", "exists" (412:
    something is already stored at that key), "http-error" or "error". With
    `repo`, the reply's Date header updates the session's clock offset."""
    import urllib.request, urllib.error
    global last_error
    t0 = time.time()
    date = None
    last_error = ""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            req = urllib.request.Request(url, data=fh, method="PUT")
            req.add_header("Content-Length", str(size))
            req.add_header("Content-Type", "application/gzip")
            # First successful upload wins. S3 and R2 honour this on PUT, so a
            # later PUT to the same key fails with 412 instead of overwriting.
            req.add_header("If-None-Match", "*")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code, date = resp.status, resp.headers.get("Date")
        result = ("ok" if 200 <= code < 300 else "http-error"), code
    except urllib.error.HTTPError as e:
        date = e.headers.get("Date") if e.headers else None
        result = ("exists" if e.code == 412 else "http-error"), e.code
        if e.code != 412:
            last_error = f"http {e.code}"
    except Exception as e:
        result = "error", None
        last_error = repr(e)[:200]
    if repo is not None:
        note_clock(repo, date, t0, time.time())
    return result


def upload(path: pathlib.Path, digest: str, quiet: bool = False) -> tuple[str, str]:
    doc = upload_doc()
    if not doc:
        return "none", ("No upload target configured. The archive is on disk; "
                        "give it to the panel directly.")
    if doc.get("_unreadable") or "url" not in doc:
        return "bad-config", "Upload target unreadable. Archive is on disk."
    if not quiet:
        print(f"\n  uploading {path.stat().st_size/1e6:.1f} MB ...")
    status, code = put(doc["url"], path, repo=ROOT)
    if status == "ok":
        return "ok", f"UPLOADED. http {code}. sha256 {digest[:16]}... You are done."
    if status == "exists":
        # Only this archive is ever sent, so this is almost always an earlier
        # attempt of ours that landed but whose reply was lost.
        return "already-submitted", (
            "The upload target already holds a submission for this session, most likely "
            f"this archive from an earlier attempt. It stands. Archive on disk at {path}.")
    what = f"http {code}" if code else f"no connection: {last_error}"
    return status, f"UPLOAD FAILED ({what}). Nothing is lost: the archive is on disk at {path}. Tell the panel."
