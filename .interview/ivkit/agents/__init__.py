"""One adapter per fully supported coding tool. The contract is in base.py.

    from ivkit import agents
    for a in agents.adapters(): ...

A module listed in NAMES but not yet on this branch is skipped; any other
import error is a bug and is raised.
"""
from __future__ import annotations
import importlib

NAMES = ("claude_code", "codex", "copilot", "cursor")


def adapters() -> list:
    out = []
    for n in NAMES:
        try:
            m = importlib.import_module(f"{__name__}.{n}")
        except ModuleNotFoundError as e:
            if e.name != f"{__name__}.{n}":
                raise
            continue
        out.append(m.ADAPTER)
    return out


def by_name(name: str):
    return next((a for a in adapters() if a.name == name), None)
