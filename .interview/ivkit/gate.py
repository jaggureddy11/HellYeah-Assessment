"""The panel's clock gate, from the kit's side.

A gated kit's payload.enc is sealed with a random key, not with the passphrase.
`interview start <passphrase>` asks the gate (gate/worker.js, a Cloudflare
Worker) for that key. The gate records when the session opened, the first time
the passphrase is right, and every answer after that carries the same opening:
the clock starts on the server the moment the passphrase is first used, and
nothing on this machine can open the exercise without asking. The model is
platform/formal/gate/Gate.tla.

Standard library only: this ships in the kit.
"""
from __future__ import annotations
import json, math, threading, time, urllib.error, urllib.request

TRIES = 4
TIMEOUT = 20.0
# Cloudflare answers urllib's own User-Agent with "error code: 1010" (a banned
# browser signature) before the Worker ever runs.
HEADERS = {"user-agent": "interview-kit/1", "content-type": "application/json"}


class GateRefused(RuntimeError):
    """The gate answered and said no: wrong passphrase, unknown session, too
    early or too late. Asking again changes nothing."""

    def __init__(self, status: int, doc: dict):
        super().__init__(f"gate refused: http {status} {doc.get('error', '')}")
        self.status, self.doc = status, doc


class GateUnreachable(RuntimeError):
    """No answer. If a request did reach the gate, the session may be open on
    the server already; asking again returns that same opening."""


def _call(req: urllib.request.Request, timeout: float | None = None, whole: bool = False) -> tuple[int, dict]:
    """One try. urllib's timeout (TIMEOUT unless given) bounds each socket
    operation, not the try: a name that resolves slowly, several addresses each
    tried in turn, or an answer that trickles in can take far longer. That is
    how /open and /health have always been asked, and still are.

    whole=True bounds the try itself: over within `timeout` seconds in all, or
    TimeoutError. It is for the calls a pausable kit's clock waits on: the
    recorder's sync, which must not hold its bell (spec §5.2: one try, 5 s),
    and pause and resume, which the candidate waits on. The request runs on a
    thread of its own; one still running at the limit is abandoned, and its
    answer never read. That is the same as an answer lost on the way, which
    those callers already handle: the gate names each event and stores it once.
    Such an answer is also read as the refusals are, as an object or {}: every
    caller takes a dict."""
    timeout = TIMEOUT if timeout is None else timeout
    if not whole:
        return _call_once(req, timeout)
    box: dict = {}

    def run():
        try:
            box["answer"] = _call_once(req, timeout)
        except Exception as e:              # handed to the caller, as if raised there
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer within {timeout:g} s")
    if "error" in box:
        raise box["error"]
    if "answer" not in box:
        # The thread ended on something that is not an Exception, which is not
        # handed on: it would pass every caller's `except Exception`, and in the
        # recorder end the loop the bell rings from. The join above waited at most
        # `timeout`; what the caller gets is what a lost answer gives it.
        raise GateUnreachable("the request ended without an answer")
    status, doc = box["answer"]
    return status, doc if isinstance(doc, dict) else {}


def _call_once(req: urllib.request.Request, timeout: float) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            doc = json.loads(e.read() or b"{}")
        except ValueError:
            doc = {}
        return e.code, doc if isinstance(doc, dict) else {}


def open_gate(url: str, session: str, passphrase: str, tries: int = TRIES, sleep=time.sleep) -> dict:
    """The gate's answer: key, opened_at, deadline (server epoch seconds),
    minutes, plus `offset`, seconds to add to this machine's clock to get the
    gate's, and `offset_err`, how far that can be from the truth (_measure). A
    request that times out or fails is sent again; an answer to an abandoned
    request is never read, and every answer carries the one stored opening, so
    a retry cannot move the clock."""
    body = json.dumps({"id": session, "passphrase": passphrase.strip()}).encode()
    last = "no attempt"
    for n in range(tries):
        if n:
            sleep(min(2 ** n, 8))
        req = urllib.request.Request(url.rstrip("/") + "/open", data=body, method="POST", headers=HEADERS)
        t0 = time.time()
        try:
            status, doc = _call(req)
        except Exception as e:                       # network: reset, timeout, DNS, TLS
            last = f"{type(e).__name__}: {e}"
            continue
        t1 = time.time()
        if status == 200:
            try:
                for k in ("key", "opened_at", "deadline", "now"):
                    doc[k] = doc[k] if k == "key" else float(doc[k])
                doc["offset"], doc["offset_err"] = _measure(doc["now"], t0, t1)
            except (KeyError, TypeError, ValueError):
                last = "the gate's answer was incomplete"
                continue
            return doc
        # only the Worker's own refusals carry an error; Cloudflare's edge can
        # answer 403 itself, before the Worker runs, and that says nothing
        # about the passphrase
        if status in (400, 403, 404, 410, 425) and doc.get("error"):
            raise GateRefused(status, doc)
        last = f"http {status}"
    raise GateUnreachable(last)


REFUSALS = (400, 403, 404, 409, 410, 425)


def _measure(now: float, t0: float, t1: float) -> tuple[float, float]:
    """The offset an answer measures, seconds to add to this machine's clock to
    get the gate's, and its error. The gate read its clock (`now`, to the
    millisecond) somewhere between this machine's t0, when the request went,
    and t1, when the answer came: the middle is off by at most half of that.

    ValueError for a `now` that is not a finite number. The Worker cannot write
    one (JSON.stringify), but Python's json reads NaN, and a NaN offset would
    make the time left NaN, which no bell rings at: an incomplete answer."""
    now = float(now)
    if not math.isfinite(now):
        raise ValueError(f"the gate's now is {now!r}")
    return now - (t0 + t1) / 2, (t1 - t0) / 2


def _event(url: str, path: str, payload: dict, tries: int, sleep) -> dict:
    """POST /pause or /resume until the gate answers. A request that timed out
    may have been stored, so sending it again is safe: the gate names the event
    (pause n, resume n) and answers a second request for it with the one it
    stored (platform/formal/pause/Pause.tla). Every answer carries the session's
    clock; a refusal carries it in GateRefused.doc. Both carry the offset the
    answer measures and its error (_measure), when the clock has a time."""
    body = json.dumps(payload).encode()
    last = "no attempt"
    for n in range(tries):
        if n:
            sleep(min(2 ** n, 8))
        req = urllib.request.Request(url.rstrip("/") + path, data=body, method="POST", headers=HEADERS)
        t0 = time.time()
        try:
            status, doc = _call(req, whole=True)
        except Exception as e:                       # network: reset, timeout, DNS, TLS
            last = f"{type(e).__name__}: {e}"
            continue
        t1 = time.time()
        if status in REFUSALS and doc.get("error"):
            if isinstance(doc.get("clock"), dict):
                try:
                    doc["offset"], doc["offset_err"] = _measure(doc["clock"]["now"], t0, t1)
                except (KeyError, TypeError, ValueError):
                    pass            # a refusal is said all the same; its clock is the caller's to judge
            raise GateRefused(status, doc)
        if status == 200 and isinstance(doc.get("clock"), dict):
            try:
                doc["offset"], doc["offset_err"] = _measure(doc["clock"]["now"], t0, t1)
            except (KeyError, TypeError, ValueError):
                last = "the gate's answer was incomplete"
                continue
            return doc
        last = f"http {status}"
    raise GateUnreachable(last)


def pause(url: str, session: str, passphrase: str, n: int, head: str,
          tries: int = TRIES, sleep=time.sleep) -> dict:
    return _event(url, "/pause", {"id": session, "passphrase": passphrase.strip(), "n": n, "head": head},
                  tries, sleep)


def resume(url: str, session: str, passphrase: str, n: int, head: str,
           tries: int = TRIES, sleep=time.sleep) -> dict:
    return _event(url, "/resume", {"id": session, "passphrase": passphrase.strip(), "n": n, "head": head},
                  tries, sleep)


def session_clock(url: str, session: str, timeout: float = 5.0) -> dict | None:
    """The session's clock from GET /session/<id>, in one try: None when the
    gate does not answer at once, or the session has no clock. No passphrase:
    the clock holds no secrets."""
    from urllib.parse import quote
    try:
        status, doc = _call(urllib.request.Request(url.rstrip("/") + "/session/" + quote(session, safe=""),
                                                   headers=HEADERS), timeout=timeout, whole=True)
    except Exception:
        return None
    c = doc.get("clock") if status == 200 else None
    return c if isinstance(c, dict) else None


def health(url: str) -> tuple[bool, str]:
    """For `interview setup`: does the gate answer from this machine?"""
    try:
        status, doc = _call(urllib.request.Request(url.rstrip("/") + "/health", headers=HEADERS))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if status == 200 and doc.get("ok"):
        return True, f"clock differs from this machine's by {float(doc['now']) - time.time():+.0f} s"
    return False, f"http {status}"
