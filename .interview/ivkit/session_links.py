"""Links to work the record cannot hold: a cloud agent's task, a web chat, a
shared thread, the export of a chat from a tool we do not read.

    ./interview link <url> [note...]

Each link is one line of .interview/session_links.jsonl, stamped in UTC by
this machine. The file is append-only like the other session logs: it rides in
every checkpoint and in the submission, so `verify` flags a line a checkpoint
held that the final submission lacks, and the digest lists every link.

The digest shows each link and note to an assessor, so both are held to plain
text: a URL with no credentials, markup or invisible characters, a note of one
line. load() applies the same checks to what is in the file, which is the
candidate's to edit, and keeps a line that fails them as
{"valid": False, "raw": <the line>, ...} for the digest to show as such.
"""
from __future__ import annotations
import datetime, json, pathlib, re, unicodedata, urllib.parse

FILE = "session_links.jsonl"
UNSAFE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")
MARKUP = re.compile(r"[<>\"`]")
# format (bidi overrides, zero-width), line and paragraph separators, lone surrogates
HIDDEN = ("Cc", "Cf", "Cs", "Zl", "Zp")
MAX_URL, MAX_NOTE = 2048, 500


class LinkError(ValueError):
    pass


def _hidden(s: str) -> bool:
    return any(unicodedata.category(c) in HIDDEN for c in s)


def check(url, note="") -> None:
    """Raises LinkError, one plain line, unless the link is one to record."""
    if not isinstance(url, str) or not isinstance(note, str):
        raise LinkError("not recorded: the link and the note must be text")
    if UNSAFE.search(url) or _hidden(url):
        raise LinkError("not recorded: the link has a space, a line break, or an invisible or control "
                        "character in it")
    if len(url) > MAX_URL:
        raise LinkError(f"not recorded: the link is longer than {MAX_URL} characters")
    if MARKUP.search(url):
        raise LinkError('not recorded: the link has a <, >, " or ` in it. Copy the link itself')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise LinkError("not recorded: give the full link, starting with https:// or http://")
    if "@" in parts.netloc:
        raise LinkError("not recorded: the link has a user name or password in it. Record a share link instead")
    if _hidden(note) or "\n" in note or "\r" in note:
        raise LinkError("not recorded: keep the note to one line of plain text")
    if len(note) > MAX_NOTE:
        raise LinkError(f"not recorded: the note is longer than {MAX_NOTE} characters")


def add(repo, url: str, note: str = "") -> dict:
    """Appends one link. Only a plain http(s) URL: anything else in the digest
    is text an assessor might click or paste."""
    check(url, note)
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "url": url, "note": note}
    d = pathlib.Path(repo) / ".interview"
    d.mkdir(parents=True, exist_ok=True)
    with (d / FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load(repo) -> list[dict]:
    """Every link recorded, oldest first, each with the line it is on. A line
    that is not a link it would record comes back marked invalid, with its
    raw text; only a torn last line, copied while being written, is skipped.
    Lines are split on newlines alone, as they were written: a separator
    character inside a note must not shift every later line's number."""
    try:
        text = (pathlib.Path(repo) / ".interview" / FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.split("\n")
    out = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            if n == len(lines):
                continue            # torn: no newline yet
            r = None
        try:
            if not isinstance(r, dict):
                raise LinkError("not a record")
            check(r.get("url"), r.get("note") or "")
            out.append({"ts": r.get("ts"), "url": r["url"], "note": r.get("note") or "", "line": n})
        except LinkError:
            d = r if isinstance(r, dict) else {}
            out.append({"ts": d.get("ts") if isinstance(d.get("ts"), str) else None,
                        "url": str(d.get("url") or ""), "note": str(d.get("note") or ""), "line": n,
                        "valid": False, "raw": line})
    return out
