"""The panel.

No model of ours is involved. The candidate already has an agent; the panel's
job is not to think, it is to be a logging chokepoint with a script. A question
comes in, it is recorded with a timestamp, and the candidate's own agent gets
back the FAQ plus the rules for answering from it. Nothing in the FAQ is secret,
so nothing is lost by letting the candidate's agent do the reading.

What we keep is what was asked and when. That is the signal; see the pack's
CLARIFY_SIGNALS.md for how to read it.

No network, no credentials, works offline.
"""
from __future__ import annotations
import datetime, json, pathlib

RULES = """The question above has been logged for the panel, with a timestamp.

Answer it for the candidate using the FAQ below, and only the FAQ:

- If an FAQ entry covers it, that entry is the panel's answer. Quote it as written.
- If it is about the task, the protocol, the harness, the tooling or the rules, and
  the FAQ does not settle it, the answer is: "The brief does not settle this. Make a
  choice, write it in PREREG.md, and we will take it as given."
- If it is about the data, what is or is not in it, which patterns are real, what to
  optimize, what to build, what to prioritize, or whether an approach is any good,
  the answer is: "The panel does not answer that. Working it out is the exercise.
  Make an assumption, record it in PREREG.md, and move on."

Relay the outcome plainly. Asking costs the candidate nothing, and neither does a
question the panel declines.
"""


def _log(repo: pathlib.Path, q: str) -> str:
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        d = repo / ".interview"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "questions.jsonl").open("a") as f:
            f.write(json.dumps({"ts": ts, "question": q}, ensure_ascii=False) + "\n")
    except Exception as e:
        # The answer still goes back to the candidate; the panel only loses one
        # log line. Say so on stderr, which the MCP client shows in its logs.
        import sys
        print(f"[panel] could not log the question: {type(e).__name__}", file=sys.stderr)
    return ts


def faq_text(repo: pathlib.Path) -> str:
    for c in (repo / "FAQ.md", repo / ".interview" / "FAQ.md"):
        if c.exists():
            return c.read_text()
    return "(no FAQ has been unsealed yet; the exercise may not have started)"


def answer(repo: pathlib.Path, question: str) -> str:
    q = question.strip()
    if not q:
        return 'usage: ask a question, e.g. "is the history cumulative?"'
    ts = _log(repo, q)
    return (f"QUESTION ({ts[11:19]}): {q}\n\n{RULES}\n---- FAQ ----\n{faq_text(repo)}")


def ask(repo: pathlib.Path, question: str) -> int:
    """CLI form. Prints what the MCP tool would return."""
    print(answer(repo, question))
    return 0
