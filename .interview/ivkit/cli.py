"""The `interview` command. One entry point for both sides of an interview.

Candidate:
    interview setup                 check this machine, before the day
    interview start <passphrase>    unseal the exercise, start the clock
    interview status                time left, snapshots taken
    interview pause                 stop the clock (a take-home you may split)
    interview resume                start it again; or restart the recorder, same deadline
    interview ask "<question>"      ask the panel (also available inside the agent)
    interview link <url> [note]     record the link to a cloud agent or web chat session
    interview submit                package and upload early
    interview mcp                   the panel as an MCP server (agents call this)

Panel:
    interview creds                 store the R2 API token, once, outside any repo
    interview packs                 list available exercises
    interview links ...             every upload link for one session, and its schedule
    interview build <pack> <pass>   make the repository to hand over
    interview presign ...           make one link
    interview notify <email>        hand one applicant a take-home kit and email it to them
    interview fetch ...             download a session's objects with R2's arrival times
    interview verify <dir>          check the checkpoints against the final submission
    interview review <repo>         summarise a finished session
    interview transcript <repo>     render the complete conversation
    interview timeline <repo>       every record of the session in order, on one clock
    interview provenance <repo>     who put each line there: agent, command, typed, pasted, completion, unknown
    interview digest <repo>         markdown for the assessors, every item cited
    interview evaluate <repo>       run the pack's own evaluation
    interview reproduce <repo>      that evaluation over the snapshots, next to the scores claimed

Cross-platform Python. Nothing here knows anything about any particular
exercise; that lives in a problem pack.
"""
from __future__ import annotations
import argparse, json, os, pathlib, shutil, subprocess, sys

from . import record, pack as packmod, paths, configs


def _repo(a) -> pathlib.Path:
    return pathlib.Path(getattr(a, "repo", None)
                        or os.environ.get("INTERVIEW_REPO") or ".").resolve()


def _problems_dir(a) -> pathlib.Path:
    return pathlib.Path(getattr(a, "dir", None)
                        or pathlib.Path(__file__).resolve().parents[2] / "problems")


# ---------------------------------------------------------------- candidate

def _mcp_selftest(repo: pathlib.Path) -> tuple[bool, str]:
    """Starts the panel server exactly as the agent will and lists its tools."""
    conf = json.loads((repo / ".mcp.json").read_text())["mcpServers"][configs.SERVER]
    env = {**os.environ, **conf.get("env", {})}
    try:
        p = subprocess.Popen([conf["command"], *conf["args"]], cwd=str(repo), env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                  "params": {}}) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        p.stdin.close()
        p.wait(timeout=5)
        tools = [t["name"] for t in json.loads(line)["result"]["tools"]]
        return True, ", ".join(tools)
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass                    # it never started, or already exited
        return False, f"{type(e).__name__}: {e}"


NOT_RECORDED = ("not fully recorded. Export your chat into this folder before the end, "
                "or record links with ./interview link")
# Recorded, but not yet checked against a real session of the tool: the brief
# asks their users to export each chat as well.
EXPORT_TOO = ("copilot", "cursor")
CURSOR_FROM = (3, 22)           # the brief's minimum: the Cursor the adapter was written against
# detect() finds a command or an app; the brief lists these VS Code extensions too.
EXTENSION = {"claude-code": "its VS Code extension is recorded too, if that is what you use",
             "codex": "its VS Code extension is recorded too, if that is what you use; "
                      "export each chat into this folder before the end as well"}


def _older(version: str, than: tuple) -> bool:
    """An editor version like 2.5.26 below `than`; anything else (installed,
    cursor-agent's own versions) is not."""
    try:
        return tuple(int(x) for x in version.split(".")[:2]) < than
    except ValueError:
        return False


def _app(name: str) -> bool:
    return paths.IS_MAC and any((d / f"{name}.app").exists()
                                for d in (pathlib.Path("/Applications"), paths.HOME / "Applications"))


def _other_tools(repo: pathlib.Path) -> list[str]:
    """Coding tools on this machine that no adapter records."""
    found = []
    if _app("Windsurf") or any(p.exists() for p in paths.editor_workspace_roots().get("Windsurf", [])):
        found.append("Windsurf")
    if shutil.which("gemini") or paths.cli_history_roots()["Gemini CLI"].exists():
        found.append("Gemini CLI")
    if shutil.which("aider") or any((repo / n).exists() for n in (".aider.chat.history.md", ".aider.input.history")):
        found.append("Aider")
    return found


def _tool_report(repo: pathlib.Path) -> list[str]:
    """One line per adapter, found or not, then one per other tool found. An
    adapter that raises gets a warning line: reading an app bundle or a
    version string on the candidate's machine must not stop setup."""
    from . import agents
    failed: dict = {}
    files = configs.local_files(repo, errors=failed)
    out = []
    for t in agents.adapters():
        try:
            v = t.detect()
        except Exception as e:
            failed.setdefault(t.name, e)
        if t.name in failed:
            out.append(f"  WARN  {t.title}: could not check ({type(failed[t.name]).__name__})")
            continue
        name = t.title if v in (None, "installed") else f"{t.title} {v}"
        if v is None:
            out.append(f"  --    {t.title} not found" + (f" ({EXTENSION[t.name]})" if t.name in EXTENSION else ""))
        elif t.supported:
            wrote = sorted(files.get(t.name, {}))
            where = f" ({', '.join(wrote)})" if wrote else ""
            if t.name == "cursor" and _older(v, CURSOR_FROM):
                out.append(f"  WARN  {name}: recorded only from Cursor 3.22 on; update it before the day{where}")
            elif t.name in EXPORT_TOO:
                out.append(f"  OK    {name}: recorded; export each chat into this folder before the end "
                           f"as well{where}")
            else:
                out.append(f"  OK    {name}: fully recorded{where}")
        else:
            out.append(f"  WARN  {name}: {NOT_RECORDED}")
    return out + [f"  WARN  {name}: {NOT_RECORDED}" for name in _other_tools(repo)]


def _hook_counts(repo: pathlib.Path) -> str:
    """How many hook records each tool has written so far: none means its
    hooks are not running."""
    from . import agents
    n: dict = {}
    try:
        with (repo / ".interview" / "transcript.jsonl").open("rb") as f:
            for raw in f:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue                # a line still being written
                if isinstance(r, dict):
                    tool = r.get("tool") if isinstance(r.get("tool"), str) else "claude-code"
                    n[tool] = n.get(tool, 0) + 1
    except OSError:
        pass                                # no hook has fired yet
    parts = [f"{t.title} {n.pop(t.name, 0)}" for t in agents.adapters()]
    return "hook records so far: " + ", ".join(parts + [f"{k} {v}" for k, v in sorted(n.items())])


def cmd_setup(a) -> int:
    repo = _repo(a)
    if paths.IS_WIN:
        print("\nNative Windows is not supported. Install WSL (in PowerShell: wsl --install),")
        print("open an Ubuntu terminal, copy this folder into your Linux home directory,")
        print("and run ./interview setup there. Your editor can open the folder through WSL.\n")
        return 1
    fails: list[str] = []
    ok = lambda m: print(f"  OK    {m}")
    bad = lambda m: (print(f"  FAIL  {m}"), fails.append(m))
    warn = lambda m: print(f"  warn  {m}")

    print("\nWhat the session tooling needs")
    print("  Only these, and only for our recording and packaging scripts.")
    print("  Your own stack, language, package manager and versions are your business.\n")
    for c in ("git", "openssl"):
        ok(c) if shutil.which(c) else bad(f"{c} not found")
    ok(f"python {sys.version_info.major}.{sys.version_info.minor} at {sys.executable}")
    sess = _session_file(repo)
    if sess.get("gate"):
        from . import gate
        up, detail = gate.health(sess["gate"])
        if up:
            ok(f"the panel's server answers ({detail}); `start` asks it to open the exercise")
        else:
            bad(f"cannot reach the panel's server at {sess['gate']} ({detail}). `start` needs it "
                f"to open the exercise"
                + (". This Python has no CA certificates: on macOS run 'Install Certificates.command' "
                   "from its folder in /Applications" if "CERTIFICATE_VERIFY_FAILED" in detail else ""))

    print("\nWiring the panel into your coding agent")
    written = configs.write_local(repo, errors={})      # a tool whose files fail is named below
    for w in written:
        ok(f"wrote {w} for this machine")
    py = configs.python_for(repo)
    if py != sys.executable:
        ok(f"the hooks and the panel keep using {py}, which the first setup chose")
    good, detail = _mcp_selftest(repo)
    if good:
        ok(f"panel server answers; tools: {detail}")
    else:
        bad(f"panel server does not start: {detail}")

    print("\nYour coding tools")
    for line in _tool_report(repo):
        print(line)
    print('  What to click in each tool before you start: "Your tools" in README.md.')

    print("\nRecorder")
    if record.started(repo):
        ok("a session is in progress here; recorder test skipped")
    elif record.alive(repo):
        bad("a recorder is already running here; setup will not touch it")
    else:
        msg = record.start(repo, minutes=5, selftest=True)
        snaps = record.snapshot_count(repo)
        probe = record.try_lock(repo)
        holds = probe is None                      # the test loop holds the lock
        if probe is not None:
            probe.close()
        if "recording" in msg and snaps >= 1 and holds:
            ok("recorder starts, snapshots and holds its lock")
        else:
            bad(f"recorder did not start properly ({snaps} snapshots, lock held: {holds}); "
                f"see .interview/recorder.log")
        record.stop(repo)
        probe = record.try_lock(repo)
        if probe is None:
            bad("the test recorder did not stop; left its files for the panel to look at")
        else:
            probe.close()
            shutil.rmtree(repo / ".interview" / "snapshots.git", ignore_errors=True)
            for f in ("recorder.json", "heartbeat", "snapshot-exclude", "stop", "recorder.log",
                      "recorder_events.jsonl"):
                (repo / ".interview" / f).unlink(missing_ok=True)
            m = record.mirror_dir(repo)
            try:
                if m and json.loads((m / "recorder.json").read_text()).get("selftest"):
                    (m / "recorder.json").unlink()
            except (OSError, ValueError):
                pass                # no self-test copy left to clear

    print("\nPermissions your operating system may ask about")
    print("  Triggered now on purpose, so nothing interrupts you during the session.")
    paths.notify("Setup is working. Allow notifications if asked.")
    print("  sent  a test notification; allow it if asked. Your time warnings use it.")
    blocked = 0
    for label, p in paths.cli_history_roots().items():
        if p.exists():
            try:
                list(p.iterdir()); ok(f"can read your {label} history")
            except Exception:
                bad(f"cannot read your {label} history at {p}"); blocked += 1
    for label, roots in paths.editor_workspace_roots().items():
        for p in roots:
            if p.exists():
                try:
                    list(p.iterdir()); ok(f"can read your {label} history")
                except Exception:
                    bad(f"cannot read your {label} history at {p}"); blocked += 1
    if blocked:
        warn("your OS is blocking one of those. Grant your terminal access to files")
        warn("      and folders, then run setup again. Only this exercise's records")
        warn("      are ever copied; read .interview/ivkit/collect.py and each tool's")
        warn("      collect() in .interview/ivkit/agents/ and see for yourself.")

    print()
    if fails:
        print("Something above needs fixing. Tell us now, not on the day.\n")
        return 1
    if sess.get("gate"):
        print("Ready. When you have the passphrase and are ready to begin, run the line below.")
        print("The clock starts the first time the panel's server accepts the passphrase.\n")
    else:
        print("Ready. At minute 0 we give you a passphrase, then run:\n")
    print("    ./interview start <passphrase>\n")
    return 0


def _session_file(repo: pathlib.Path) -> dict:
    """.interview/session.json: what the kit knows before it is opened, the
    take-home's times and the clock gate. Empty when there is none."""
    try:
        s = json.loads((repo / ".interview" / "session.json").read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _gate_refusal(e) -> str:
    import datetime
    local = lambda t: datetime.datetime.fromtimestamp(float(t)).strftime("%a %d %b %H:%M")
    if e.status == 403:
        return "That passphrase did not work. Check it with the panel."
    if e.status == 425 and e.doc.get("opens_at"):
        return f"This session opens at {local(e.doc['opens_at'])} your time. Run start again then."
    if e.status == 410 and e.doc.get("open_until"):
        return (f"This session could be opened until {local(e.doc['open_until'])} your time, and that "
                f"has passed. Tell the panel.")
    if e.status == 404:
        return "The panel's server does not know this kit. Tell the panel."
    return f"The panel's server refused to open the exercise ({e}). Tell the panel."


def cmd_start(a) -> int:
    repo = _repo(a)
    if paths.IS_WIN:
        print("Native Windows is not supported. Run this kit inside WSL.")
        return 1
    import datetime, time
    sess = _session_file(repo)
    if record.started(repo):
        st = record.state(repo)
        clock_set = st.get("deadline") and not st.get("selftest")
        if clock_set and sess.get("pausable") and sess.get("gate"):
            return _rejoin(repo, sess, a.passphrase)
        if not (sess.get("gate") and not clock_set):
            began = f" at {datetime.datetime.fromtimestamp(st['started']):%H:%M}" if st.get("started") else ""
            print(f"This session already started{began}. The clock does not restart.")
            print("  ./interview status    time left (and restarts the recorder if it stopped)")
            return 1
        # Unsealed, but the start died before it set the clock. The gate holds the
        # opening, so asking it again cannot buy time: pick its clock up.
        print("The exercise was opened here but the clock was never set; asking the panel's server for it.")
    from . import seal, submit
    try:
        closes = record.parse_iso(sess["deadline"])
    except Exception:
        closes = None
    # a gated kit asks the gate, whose clock is the one that counts: this machine's may be fast
    if closes is not None and not sess.get("gate") and closes.timestamp() <= time.time() - 300:
        print(f"This session closed at {closes.astimezone():%H:%M} your time. Tell the panel.")
        return 1
    opened = None
    if sess.get("gate"):
        if getattr(a, "minutes", None):
            print("This kit's clock is the panel's server's. For a rehearsal, build the kit with --minutes.")
            return 1
        from . import gate
        print("Asking the panel's server to open the exercise...")
        try:
            opened = gate.open_gate(sess["gate"], sess["id"], a.passphrase)
        except gate.GateRefused as e:
            print(_gate_refusal(e))
            return 1
        except gate.GateUnreachable as e:
            print(f"Could not reach the panel's server ({e}). Check your internet connection and run")
            print("the same command again. If an earlier try did get through, your clock is already")
            print("running on the server, and running start again picks that clock up.")
            return 1
        if opened["deadline"] <= time.time() + opened["offset"]:
            print(f"The panel's server opened this session at "
                  f"{datetime.datetime.fromtimestamp(opened['opened_at'] - opened['offset']):%H:%M} your time, "
                  f"and its time has run out. Tell the panel.")
            return 1
    try:
        meta = seal.unseal(repo, opened["key"] if opened else a.passphrase)
    except seal.SealError as e:
        print("The panel's server answered, but its key does not open this kit. Tell the panel."
              if opened else str(e))
        return 1
    mins = float(meta.get("build_minutes", 60))
    if getattr(a, "minutes", None):
        mins = float(a.minutes)
        print(f"REHEARSAL: deadline overridden to {mins:g} minutes (the pack says "
              f"{meta.get('build_minutes', 60)}).")
    doc = submit.upload_doc(repo)
    sched = doc.get("schedule") or {}
    deadline = base = None
    if opened:
        # The server's opening, whenever this answer arrived: a start that is run
        # again, or retried after a lost answer, gets the same clock (Gate.tla).
        offset = opened["offset"]
        deadline = opened["deadline"]
        base = record.parse_iso(sched["start"]).timestamp() if sched.get("start") else opened["opened_at"]
    else:
        offset = submit.server_offset(doc["url"]) if doc.get("url") else None
    if sched.get("deadline") and not opened and not getattr(a, "minutes", None):
        # The panel's clock, not this machine's: the storage server's Date header.
        deadline = record.parse_iso(sched["deadline"]).timestamp()
        base = record.parse_iso(sched["start"]).timestamp() if sched.get("start") else None
        if deadline <= time.time() + (offset or 0):
            print("This session has already closed. Tell the panel.")
            return 1
        if offset is None:
            print("Could not reach the storage server to check this computer's clock; using it as it is.")
    if deadline is None and offset is not None and not getattr(a, "minutes", None):
        # A live session too runs on the server's clock once it can be read:
        # a clock corrected mid-session then moves neither the bell nor the checkpoints.
        base = time.time() + offset
        deadline = base + mins * 60
    seal.write_exclude(repo)
    print(record.start(repo, minutes=mins, deadline=deadline, checkpoint_base=base,
                       clock_offset=offset or 0.0, offset_measured=offset is not None))
    if (repo / ".git").exists():
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
        subprocess.run(["git", "-c", "user.name=panel", "-c", "user.email=panel@local",
                        "commit", "-qm", "unpack exercise, clock starts"],
                       cwd=repo, capture_output=True)
    if opened and isinstance(opened.get("clock"), dict):
        # A pausable session. Keep the passphrase, so that pause and resume take
        # no argument, and the kit's copy of the server's clock, whose bell is
        # recorder.json's deadline (spec §5.1). An error writing gate.json (it is
        # a symlink: the kit was tampered with) fails the start.
        from . import pause
        pause.keep_passphrase(repo, a.passphrase)
        try:
            pause.apply(repo, opened["clock"])
        except (ValueError, TimeoutError) as e:
            # The recorder already runs on opened["deadline"], which is the bell:
            # the session goes on, and the next answer that can be read fills
            # the ledger in. The event tells the panel why it was empty.
            record.event(repo, "clock_unreadable", by="start", error=f"{type(e).__name__}: {e}"[:300])
    st = record.state(repo)                  # the recorder sends the start marker on its first tick
    now = datetime.datetime.now()
    if (sched.get("deadline") or opened) and deadline is not None:
        left = int(record.time_left(repo, st))
        end = datetime.datetime.fromtimestamp(time.time() + left)
        began = now
        if opened:
            began = datetime.datetime.fromtimestamp(opened["opened_at"] - opened["offset"])
            if (now - began).total_seconds() > 60:
                print(f"\nThe panel's server opened this session at {began:%H:%M}, the first time the "
                      f"passphrase was used; the clock has run since.")
        print(f"\nClock started at {began:%H:%M:%S}. The session ends at {end:%H:%M} your time, "
              f"set by the panel; you have {left // 3600}h {left % 3600 // 60:02d}m.\n")
    else:
        print(f"\nClock started at {now:%H:%M:%S}. You have {mins:g} minutes.\n")
    if doc.get("checkpoints"):
        every = sched.get("every_minutes") or doc["checkpoints"][0].get("offset_minutes")
        print(f"  A checkpoint of the recording is uploaded every {every:g} min.")
    if sess.get("pausable"):
        one = mins == 60
        print(f"  Your {mins / 60:g} {'hour is' if one else 'hours are'} working time, and you can split "
              f"{'it' if one else 'them'}:")
        print("  ./interview pause     stops your clock (needs the internet)")
        print("  ./interview resume    starts it again")
        print("  Do not work on the exercise while paused.\n")
    print(f"  {meta.get('brief', 'BRIEF.md')}       the exercise")
    print("  ask your agent to ask the panel, or: ./interview ask \"...\"")
    print("  ./interview status    time left\n")
    return 0


def _rejoin(repo: pathlib.Path, sess: dict, passphrase: str) -> int:
    """`start` run again on a pausable kit that has started (spec §5.3): picks up
    the server's clock and changes nothing else. Nothing is unsealed again and
    the recorder's state is not reset. It brings a dead recorder back, as any
    command does, before it asks the server; on a kit already submitted it only
    reports the status, and once the bell has rung it says so rather than
    offering a pause or a resume."""
    import time
    from . import gate, pause, submit
    # Running the recorder is not resetting it, and needs no server, so it is
    # brought back first: a candidate rerunning start offline, after a reboot,
    # is recorded again. The label is not "start", which verify and digest
    # read as the session's first start and leave out: this restart, and the
    # gap before it, must show.
    if record.ensure(repo, "start-again"):
        print("The recorder was not running. Restarted it on the server's clock.")
    if submit.receipt(repo):
        print(f"This session started earlier: {record.status(repo)}")
        return 0
    try:
        opened = gate.open_gate(sess["gate"], sess["id"], passphrase)
    except gate.GateRefused as e:
        print(_gate_refusal(e))
        return 1
    except gate.GateUnreachable as e:
        print(f"Could not reach the panel's server ({e}). Your clock here is as it was; run the same "
              f"command again when you are online.")
        return 1
    pause.keep_passphrase(repo, passphrase)
    try:
        if isinstance(opened.get("clock"), dict):
            # The offset this answer measured goes in with its clock, before the
            # bell moves: the machine's clock may have been corrected since the
            # last sitting (pause.apply).
            st, _ = pause.apply(repo, opened["clock"], _measured(opened))
        else:
            st = pause.load(repo)
    except (ValueError, TimeoutError) as e:
        record.event(repo, "clock_unreadable", by="start-again", error=f"{type(e).__name__}: {e}"[:300])
        print("The panel's server answered, but its clock could not be read here. Tell the panel.")
        return 1
    now = time.time() + opened["offset"]
    if st and st["bell"] <= now:
        # The bell has rung on the server's clock: the end of the budget while
        # running, the window's end while paused (spec §5.4), after which the
        # gate refuses a resume. The recorder, brought back above if it had
        # died, submits; describe() would offer a pause or a resume that cannot
        # happen.
        why = "its time is up" if st["phase"] == "running" else "its window has closed"
        print(f"This session started earlier, and {why}. The recorder is packaging and uploading your work "
              f"as it stands; ./interview status shows when it has landed.")
        return 0
    print(f"This session started earlier. It is {pause.describe(st, now, opened['offset'])}." if st else
          "This session started earlier.")
    return 0


def cmd_status(a) -> int:
    repo = _repo(a)
    if record.ensure(repo, "status"):
        print("The recorder was not running. Restarted it on the original deadline.")
        import time
        time.sleep(2)
    # A pausable kit asks the server first (spec §5.2): its clock may have moved
    # from another terminal, or by an answer this kit never got. The sync
    # changes only the kit's copy, and never starts or stops the clock.
    from . import pause
    heard = True
    if pause.session(repo):
        _, moved, heard = pause.sync_heard(repo)
        if moved:
            print(f"The panel's server says your clock is {moved} now.")
    # With no answer it could read, the clock is the kit's copy, said as the
    # last known (spec §5.2).
    print(record.status(repo, heard=heard))
    print(_hook_counts(repo))
    print('  0 for a tool you use, after your first prompt: its hooks are not approved or not running.'
          ' Its steps are under "Your tools" in the brief.')
    return 0


# Spec §5.2: a request that got no answer leaves the command not knowing what the
# server did. The gate names each event and stores it once, so running the
# command again is safe; the candidate is never told a phase the server has not
# said.
NO_ANSWER = ("Could not reach the panel's server ({why}). Your clock may or may not be {what} now: "
             "run the same command again, it is safe.")


def _pause_refusal(e, what: str | None) -> str:
    """What a refusal from /pause or /resume means for the candidate: the
    Worker's refusals (spec §4.2, gate/worker.js step). `what` describes the
    clock the refusal carried, already applied, or is None when it carried none.
    time_up is neutral: /pause and /resume both answer it. The 410s say the
    recorder is submitting: the clock applied puts its bell in the past, and the
    command makes sure a recorder runs before it returns."""
    if e.status == 410 and e.doc.get("error") == "time_up":
        return "Your time is up: none of your budget is left. The recorder is submitting your work."
    if e.status == 410 and e.doc.get("error") == "closed":
        return "The window for this exercise has ended. The recorder is submitting your work."
    if e.status == 409 and e.doc.get("error") == "too_many":
        return "This session has been paused 20 times, the most it can be. Tell the panel."
    if e.status == 409 and what:
        return f"The panel's server says the session is not where this kit thought; your clock is {what}."
    if e.status == 403:
        return "The panel's server did not accept this kit's passphrase. Tell the panel."
    return f"The panel's server refused ({e}). Tell the panel."


NOT_PAUSABLE = "This kit's clock cannot be paused."
SUBMITTED = "This session has been submitted; its clock is done."
NOT_STARTED = "This kit has not been started here. Run ./interview start <passphrase> first."
# A started pausable kit whose kept passphrase is gone. gate.json is ignored by
# the kit's .gitignore and, being the secret, has no second copy under .git, so
# `git clean -fdx` deletes it. Start run again on a started pausable kit keeps
# the passphrase anew and picks up the server's clock without resetting
# anything (spec §5.3, cli._rejoin), which is the way out. The name says the
# file, not the secret: CodeQL takes a name with "passphrase" in it for the
# passphrase itself, and every print of this text for a leak.
GATE_FILE_GONE = ("This kit no longer holds your passphrase (.interview/gate.json is gone; git clean -x "
                  "deletes it). Nothing was sent to the panel's server, so this command changed nothing "
                  "on your clock. Run ./interview start <passphrase> again: it keeps the passphrase and "
                  "picks up the same clock. Then run ./interview {cmd}.")


def _why_not_pausable(repo: pathlib.Path, cmd: str) -> str | None:
    """Why this is not a started pausable kit that can ask the server, or None
    when it is: NOT_PAUSABLE, SUBMITTED, NOT_STARTED, or GATE_FILE_GONE filled in
    with `cmd`, the command asking. It only says why, and never returns the
    passphrase: what it returns is printed, and the secret must never be
    (spec §5.1), so the caller reads the passphrase itself (_pausable_kit)."""
    from . import pause, submit
    if not pause.session(repo):
        return NOT_PAUSABLE
    if submit.receipt(repo):
        return SUBMITTED
    if pause.passphrase(repo):
        return None
    # Started is what `start` itself goes by to tell a rerun (cmd_start): the
    # clock set in recorder.json, which record.state puts back from its mirror
    # in .git/interview/, or the exercise unsealed, whose files are tracked.
    # Both outlive a `git clean -fdx`; the kept passphrase does not.
    if record.started(repo):
        return GATE_FILE_GONE.format(cmd=cmd)
    return NOT_STARTED


def _pausable_kit(repo: pathlib.Path):
    """(session.json, passphrase) of a kit _why_not_pausable passed, or None if
    the passphrase went in between; the caller then says GATE_FILE_GONE."""
    from . import pause
    pw = pause.passphrase(repo)
    return (pause.session(repo), pw) if pw else None


def _measured(doc: dict) -> tuple[float, float] | None:
    """The offset a gate answer measured and its error (gate._measure), for
    pause.apply to keep before it moves the bell; None if the answer has none."""
    try:
        return float(doc["offset"]), float(doc["offset_err"])
    except (KeyError, TypeError, ValueError):
        return None


def _send(repo: pathlib.Path, kind: str, sess: dict, pw: str, n: int, head: str):
    """Sends `pause n` or `resume n` and applies the clock in the answer, or in
    a refusal. Returns (state, offset), or None after saying why there is
    none: a refusal, no answer, or a clock this kit could not take in. The
    offset, for both, is the one in force once the answer is applied
    (record.offset), the one the recorder times the bell with: the answer's own
    measurement, or a more precise one it agreed with (pause.apply)."""
    import time
    from . import gate, pause
    send = gate.pause if kind == "pause" else gate.resume
    try:
        ans = send(sess["gate"], sess["id"], pw, n, head)
    except gate.GateRefused as e:
        clock = e.doc.get("clock")
        if not isinstance(clock, dict):
            print(_pause_refusal(e, None))
            return None
        try:
            now_st = pause.apply(repo, clock, _measured(e.doc))[0]
        except (ValueError, TimeoutError) as err:
            record.event(repo, "clock_unreadable", by=kind, error=f"{type(err).__name__}: {err}"[:300])
            print(f"The panel's server refused ({e}), and its clock could not be taken in here. Tell the panel.")
            return None
        off = record.offset(repo, record.state(repo))
        print(_pause_refusal(e, pause.describe(now_st, time.time() + off, off) if now_st else None))
        return None
    except gate.GateUnreachable as e:
        print(NO_ANSWER.format(why=e, what="paused" if kind == "pause" else "running"))
        return None
    try:
        st, _ = pause.apply(repo, ans["clock"], _measured(ans))
    except (ValueError, TimeoutError) as err:
        # The gate answered 200, so it holds the event, or the session has moved
        # past it; which phase it is in now, this kit cannot say (spec §5.2).
        record.event(repo, "clock_unreadable", by=kind, error=f"{type(err).__name__}: {err}"[:300])
        print(f"The panel's server answered, but its clock could not be taken in here ({err}). Tell the panel.")
        return None
    return st, record.offset(repo, record.state(repo))


def cmd_pause(a) -> int:
    """Spec §5.3: sync, snapshot, send `pause n` naming that snapshot, apply the
    answer. Run again after a lost answer, it finds the pause stored."""
    repo = _repo(a)
    why = _why_not_pausable(repo, "pause")
    kit = None if why else _pausable_kit(repo)
    if not kit:
        why = why or GATE_FILE_GONE.format(cmd="pause")
        print(why)
        if why not in (NOT_PAUSABLE, SUBMITTED, NOT_STARTED):
            # The passphrase is gone from a started kit: nothing can be sent,
            # but the bell still needs a running recorder (spec §5.4), as below.
            record.ensure(repo, "pause")
        return 1
    try:
        return _pause(repo, *kit)
    finally:
        # Spec §5.4: the bell, which is the window's end while paused, needs a
        # running recorder, and across a pause nothing else restarts it. So
        # every way out of `pause`, a refusal or no answer too, makes sure one
        # runs. After the snapshot, so that one is never taken two ways at once.
        record.ensure(repo, "pause")


def _pause(repo: pathlib.Path, sess: dict, pw: str) -> int:
    import time
    from . import pause
    st, _ = pause.sync(repo)
    if st.get("phase") == "paused":
        # The copy says paused n; the server may have moved on since (a resume
        # stored elsewhere, or one whose answer was lost), and the sync may have
        # gone unanswered. Ask with `pause n` (spec §5.2): the gate answers a
        # stored pause with the clock as it is now and stores nothing, never
        # reading the head (gate/worker.js step). The latest snapshot is named
        # all the same, without taking a new one: were the server ever behind
        # this copy, the pause it stored would still name a snapshot.
        got = _send(repo, "pause", sess, pw, int(st["n"]), record.head(repo))
        if got is None:
            return 1
        st, off = got
        now = time.time() + off
        if st.get("phase") == "paused":
            print(f"Already paused. {pause.used(st, now)}. Your clock is {pause.describe(st, now, off)}.")
            return 0
        # Running on the server: the pause asked for is of the segment it is in.
    head = pause.snapshot(repo)
    got = _send(repo, "pause", sess, pw, int(st.get("n") or 1), head)
    if got is None:
        return 1
    st, off = got
    now = time.time() + off
    if st.get("phase") != "paused":
        print(f"The panel's server says your clock is {pause.describe(st, now, off)}.")
        return 1
    paths.notify("Paused. Your clock has stopped.")
    print(f"Paused. {pause.used(st, now)}. Your clock is {pause.describe(st, now, off)}.")
    return 0


def cmd_resume(a) -> int:
    """Spec §5.3: on a paused kit, sync, snapshot, send `resume n`, apply the
    answer and make sure the recorder runs. On any other kit, or one that is
    not paused, it restarts the recorder as it always has. On a started
    pausable kit that has lost its passphrase it says so and fails: it cannot
    ask the server, and saying "resumed" would tell the candidate something the
    server has not said (spec §5.2) while their clock stays paused."""
    repo = _repo(a)
    why = _why_not_pausable(repo, "resume")
    if why in (NOT_PAUSABLE, SUBMITTED, NOT_STARTED):
        print(record.resume(repo))          # not pausable, over, or never started: the recorder, as before
        return 0
    kit = None if why else _pausable_kit(repo)
    if not kit:
        print(why or GATE_FILE_GONE.format(cmd="resume"))
        record.ensure(repo, "resume")       # spec §5.4, as in the finally below
        return 1
    try:
        return _resume(repo, *kit)
    finally:
        # Spec §5.4, as in cmd_pause: a refused or unanswered resume, after a
        # reboot between sittings, must not leave the work unrecorded.
        record.ensure(repo, "resume")


def _resume(repo: pathlib.Path, sess: dict, pw: str) -> int:
    import time
    from . import pause
    st, moved, heard = pause.sync_heard(repo)
    off = record.offset(repo, record.state(repo))
    now = time.time() + off
    if st.get("phase") != "paused":
        # Not paused: the recorder, as before. What is said of the clock is what
        # the server said (spec §5.2): that it has moved to running, when this
        # is a resume run again after a lost answer; or that it could not be asked.
        if not heard:
            print("Could not confirm your clock with the panel's server"
                  + (f"; the last it said here: {pause.describe(st, now, off)}" if st else "")
                  + ". Run the same command again when you are online.")
            print(record.resume(repo))
            return 1
        if moved == "running":
            print(f"The panel's server says your clock is {pause.describe(st, now, off)}.")
        print(record.resume(repo))
        return 0
    head = pause.snapshot(repo)
    got = _send(repo, "resume", sess, pw, int(st["n"]), head)
    if got is None:
        return 1
    st, off = got
    now = time.time() + off
    if st.get("phase") != "running":
        # A 200 from /resume is not by itself a running clock: the clock in the
        # answer says where the session stands, and the candidate is told what
        # the server said, never what this kit hoped (spec §5.2). As for pause.
        print(f"The panel's server says your clock is {pause.describe(st, now, off)}.")
        return 1
    print(f"Resumed. Your clock is {pause.describe(st, now, off)}.")
    return 0


def cmd_ask(a) -> int:
    from . import panel
    record.ensure(_repo(a), "ask")
    return panel.ask(_repo(a), " ".join(a.question))


def cmd_link(a) -> int:
    from . import session_links
    try:
        session_links.add(_repo(a), a.url, " ".join(a.note))
    except session_links.LinkError as e:
        print(str(e))
        return 2
    except (OSError, UnicodeError) as e:
        print(f"not recorded: could not write .interview/{session_links.FILE} ({type(e).__name__}). "
              f"Tell the panel.")
        return 1
    print("OK    link recorded")
    return 0


def cmd_submit(a) -> int:
    from . import submit
    r = submit.run(_repo(a))
    return 0 if r.get("upload") in submit.DONE else 2


def cmd_collect(a) -> int:
    from . import collect
    repo = _repo(a)
    lock = record.try_lock(repo)
    if lock is None:
        # The recorder may be packaging from agent_logs right now: preview elsewhere.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            print("(the recorder is running; this is a preview, nothing is written here)")
            collect.run(repo, out=pathlib.Path(tmp) / "agent_logs")
        return 0
    try:
        collect.run(repo)
    finally:
        lock.close()
    return 0


def cmd_mcp(a) -> int:
    os.environ.setdefault("INTERVIEW_REPO", str(_repo(a)))
    from . import panel_mcp
    return panel_mcp.main()


# ---------------------------------------------------------------- panel

def cmd_packs(a) -> int:
    base = _problems_dir(a)
    found = packmod.discover(base)
    if not found:
        print(f"no problem packs under {base}")
        return 1
    for p in found:
        print(p.describe() + "\n")
    return 0


def cmd_build(a) -> int:
    from . import seal
    src = pathlib.Path(a.pack)
    p = packmod.Pack.load(src if src.exists() else _problems_dir(a) / a.pack)
    out = pathlib.Path(a.out or f"build/{p.name}-kit").resolve()
    if a.gate_record and not a.gate:
        print("build: --gate-record without --gate: there is no gate to write a record for", file=sys.stderr)
        return 1
    try:
        seal.build(p, out, a.passphrase,
                   upload=pathlib.Path(a.upload) if a.upload else None, minutes=a.minutes, world=a.world,
                   gate=a.gate, gate_out=pathlib.Path(a.gate_record) if a.gate_record else None,
                   pausable=a.pausable)
    except seal.SealError as e:
        print(f"build: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_presign(a) -> int:
    from . import presign
    argv = ["--bucket", a.bucket, "--key", a.key, "--hours", str(a.hours),
            "--method", a.method]
    if a.endpoint:
        argv += ["--endpoint", a.endpoint]
    if a.out:
        argv += ["-o", a.out]
    if a.url_only:
        argv += ["--url-only"]
    if a.immutable:
        argv += ["--immutable"]
    return presign.main(argv)


def cmd_verify(a) -> int:
    from . import verify
    return verify.main([a.dir])


def cmd_creds(a) -> int:
    from . import creds
    return creds.prompt_and_store(show=a.show)


def cmd_review(a) -> int:
    from . import review
    review.main(str(_repo(a)))
    return 0


def cmd_transcript(a) -> int:
    from . import render
    return render.main([str(_repo(a))] + (["--brief"] if a.brief else []))


def cmd_evaluate(a) -> int:
    from . import evaluate
    return evaluate.run(_repo(a), a.pack, a.seeds, a.extra)


def is_candidate_kit() -> bool:
    return (pathlib.Path(__file__).resolve().parent / "_role").exists()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="interview", description="interview platform")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    s = add("setup", cmd_setup, "check this machine"); s.add_argument("--repo")
    s = add("start", cmd_start, "unseal and start the clock")
    s.add_argument("passphrase"); s.add_argument("--repo")
    if not is_candidate_kit():
        # A candidate's clock is sealed into the kit (`build --minutes` for a
        # rehearsal); only the panel's own checkout can override it here.
        s.add_argument("--minutes", type=float, help="panel checkout only: override the pack's clock")
    s = add("status", cmd_status, "time left"); s.add_argument("--repo")
    s = add("pause", cmd_pause, "stop the clock (a take-home you may split)"); s.add_argument("--repo")
    s = add("resume", cmd_resume, "start the clock again; or restart the recorder after a sleep or crash")
    s.add_argument("--repo")
    s = add("ask", cmd_ask, "ask the panel"); s.add_argument("question", nargs="+")
    s.add_argument("--repo")
    s = add("link", cmd_link, "record the link to a cloud agent or web chat session")
    s.add_argument("url"); s.add_argument("note", nargs="*"); s.add_argument("--repo")
    s = add("submit", cmd_submit, "package and upload"); s.add_argument("--repo")
    s = add("collect", cmd_collect, "gather agent chat history"); s.add_argument("--repo")
    s = add("mcp", cmd_mcp, "serve the panel over MCP (agents call this)")
    s.add_argument("--repo")

    if is_candidate_kit():
        return ap

    s = add("packs", cmd_packs, "list problem packs"); s.add_argument("--dir")
    s = add("build", cmd_build, "build the kit to hand over")
    s.add_argument("pack"); s.add_argument("passphrase")
    s.add_argument("--dir"); s.add_argument("--out"); s.add_argument("--upload")
    s.add_argument("--minutes", type=float, help="seal a shorter clock into the kit, for rehearsals")
    s.add_argument("--world", type=int, help="one of the pack's world_seeds: seal that world's data (handover.zsh)")
    s.add_argument("--gate", help="the clock gate's URL: seal with a random key the gate hands out at start")
    s.add_argument("--gate-record", help="with --gate: where to write the record the gate reads (holds the key)")
    s.add_argument("--pausable", action="store_true",
                   help="with --gate: the minutes are a budget the candidate may pause (notify's take-homes)")
    s = add("creds", cmd_creds, "store the R2 API token outside any repo (prompts)")
    s.add_argument("--show", action="store_true", help="show where it is and what is set")
    s = add("presign", cmd_presign, "make an upload link")
    s.add_argument("--endpoint", help="default derived from R2_ACCOUNT_ID")
    for f in ("bucket", "key"):
        s.add_argument(f"--{f}", required=True)
    s.add_argument("--hours", type=float, default=8); s.add_argument("-o", "--out")
    s.add_argument("--method", default="PUT", choices=["PUT", "GET"])
    s.add_argument("--url-only", action="store_true")
    s.add_argument("--immutable", action="store_true",
                   help="PUT only: sign If-None-Match: * into the URL; the object can never be overwritten")
    add("links", None, "every upload link for one session, and its schedule (see links --help)")
    add("fetch", None, "download a session's submission and checkpoints (see fetch --help)")
    add("notify", None, "hand one applicant a take-home kit and email it (see notify --help)")
    s = add("verify", cmd_verify, "check the checkpoints against the final submission")
    s.add_argument("dir")
    s = add("review", cmd_review, "summarise a session"); s.add_argument("repo")
    s = add("transcript", cmd_transcript, "render the full conversation")
    s.add_argument("repo"); s.add_argument("--brief", action="store_true")
    add("timeline", None, "every record of the session in order, on one clock (see timeline --help)")
    add("provenance", None, "who put each line there (see provenance --help)")
    add("digest", None, "markdown digest for the assessors (see digest --help)")
    s = add("evaluate", cmd_evaluate, "run the pack's evaluation on a repo")
    s.add_argument("repo"); s.add_argument("--pack"); s.add_argument("--seeds", type=int, default=20)
    s.add_argument("extra", nargs="*",
                   help="anything after a bare -- is passed to the pack's command")
    add("reproduce", None, "the evaluation over the snapshots, next to the scores claimed (see reproduce --help)")
    return ap


# Commands with their own option parser: everything after the name is theirs.
PASSTHROUGH = {"links": ("links", "main"), "fetch": ("verify", "fetch_main"), "timeline": ("trace", "main"),
               "notify": ("notify", "main"),
               "provenance": ("provenance", "main"), "digest": ("digest", "main"),
               "reproduce": ("reproduce", "main")}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] and argv[0] in PASSTHROUGH and not is_candidate_kit():
        import importlib
        mod, fn = PASSTHROUGH[argv[0]]
        return getattr(importlib.import_module(f"ivkit.{mod}"), fn)(argv[1:])
    # `evaluate <repo> --seeds 12 -- --perturb x`: before 3.12, argparse gives the
    # empty `extra` to the positional pass and then rejects what follows `--`, so
    # everything after it is split off here and handed over as is.
    tail = []
    if argv[:1] == ["evaluate"] and "--" in argv:
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    a = build_parser().parse_args(argv)
    if getattr(a, "extra", None) is not None:
        a.extra = [x for x in a.extra if x != "--"] + tail
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
