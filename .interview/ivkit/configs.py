"""The files that wire the session into the candidate's coding agent.

Each tool with an adapter (ivkit/agents/) names its own: its project hook
config, every hook calling hook_log.py --tool <name>, and its panel MCP entry.
Claude Code's are .claude/settings.json and .mcp.json; the VS Code extension
loads project settings too, so it is covered. A tool with no adapter gets
nothing here.

No two tools may name the same file, and no tool its hooks and its MCP entry in
one: the second would silently replace the first. That is PathClash.

Two flavours of each:

  defaults   written at build time, before we know the candidate's machine.
             Use the agent's own variable for the project root and plain
             `python3`. Work on macOS and Linux out of the box. An adapter is
             given the scripts' repo-relative paths and repo_root None.

  local      written by `interview setup` on the candidate's machine, once we
             know the absolute path and the interpreter that actually exists.
             Correct on Windows too, where `python3` is not a command. An
             adapter is given absolute paths and the repository root.

The interpreter local files name is the one the first `interview setup` ran
under, recorded in .interview/python and reused while it still exists. Setup
can run again from a shell with another python first on PATH, or through the
launcher's uv fallback; without the record each such run would rewrite every
hook file, and a tool that trusts a hook by the hash of its definition (Codex's
.codex/hooks.json) would silently stop running it. So for one repository an
adapter's hook_files must depend only on its arguments: the same interpreter,
scripts and root give the same bytes.

Relative paths are not used anywhere: MCP clients do not guarantee the server's
working directory is the project root.
"""
from __future__ import annotations
import json, os, pathlib, sys

from . import agents

SERVER = "interview-panel"
MCP_REL = ".interview/ivkit/panel_mcp.py"
HOOK_REL = ".interview/ivkit/hook_log.py"
PYTHON_REL = ".interview/python"


class PathClash(ValueError):
    """Two tools, or one tool's hooks and its MCP entry, name the same file."""


def _mcp(command: str, script: str, repo_env: str) -> str:
    return json.dumps({"mcpServers": {SERVER: {
        "command": command,
        "args": [script],
        "env": {"INTERVIEW_REPO": repo_env},
    }}}, indent=2) + "\n"


def _files(py: str, mcp_script: str, hook_script: str, root: str | None,
           errors: dict | None = None) -> dict[str, dict[str, str]]:
    """{tool: {repo-relative path: content}}, every adapter's. A tool that
    raises, or clashes with one before it, raises here; given `errors`, it
    goes into errors[name] instead and its files are left out, so one tool
    never stops the candidate's setup."""
    tools = [(a.name, lambda a=a: (a.mcp_files(py, mcp_script, root), a.hook_files(py, hook_script, root)))
             for a in agents.adapters()]
    out: dict[str, dict[str, str]] = {}
    owner: dict[str, str] = {}
    for name, files in tools:
        try:
            mcp, hooks = files()
            both = sorted(set(mcp) & set(hooks))
            if both:
                raise PathClash(f"{name} writes its hooks and its MCP entry both into {both[0]}")
            taken = sorted(p for p in {**mcp, **hooks} if p in owner)
            if taken:
                raise PathClash(f"{name} and {owner[taken[0]]} both write {taken[0]}")
        except Exception as e:
            if errors is None:
                raise
            errors[name] = e
            continue
        out[name] = {**mcp, **hooks}
        owner.update(dict.fromkeys(out[name], name))
    return out


def _write(repo: pathlib.Path, files: dict[str, dict[str, str]]) -> list[str]:
    out = []
    for per_tool in files.values():
        for rel, text in per_tool.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            out.append(str(path.relative_to(repo)))
    return out


def write_defaults(repo: pathlib.Path) -> None:
    _write(repo, _files("python3", MCP_REL, HOOK_REL, None))


def python_for(repo: pathlib.Path) -> str:
    """The interpreter the first setup recorded, while it is still there;
    otherwise this one."""
    try:
        py = (repo / PYTHON_REL).read_text().strip()
    except (OSError, ValueError):
        py = ""                             # no setup yet, or a record we cannot read
    return py if py and os.path.isfile(py) and os.access(py, os.X_OK) else sys.executable


def local_files(repo: pathlib.Path, interpreter: str | None = None,
                errors: dict | None = None) -> dict[str, dict[str, str]]:
    """{tool: {repo-relative path: content}} as write_local writes them."""
    py = interpreter or python_for(repo)
    root = str(repo.resolve())
    sep = "\\" if sys.platform.startswith("win") else "/"
    mcp_script = root + sep + MCP_REL.replace("/", sep)
    hook_script = root + sep + HOOK_REL.replace("/", sep)
    return _files(py, mcp_script, hook_script, root, errors)


def write_local(repo: pathlib.Path, interpreter: str | None = None, errors: dict | None = None) -> list[str]:
    """Rewrites every tool's files for this machine and records the
    interpreter they name. Returns what it wrote."""
    py = interpreter or python_for(repo)
    written = _write(repo, local_files(repo, py, errors))
    (repo / PYTHON_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PYTHON_REL).write_text(py + "\n")
    return written
