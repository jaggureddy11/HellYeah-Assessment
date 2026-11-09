"""Where each coding tool keeps its history, per operating system.

Everything platform-specific in this package lives here. Adding an operating
system or a new tool means editing this file and nothing else.
"""
from __future__ import annotations
import os, sys, pathlib

HOME = pathlib.Path.home()
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def app_support() -> list[pathlib.Path]:
    """Roots under which editors keep per-workspace state."""
    if IS_WIN:
        appdata = os.environ.get("APPDATA") or str(HOME / "AppData" / "Roaming")
        return [pathlib.Path(appdata)]
    if IS_MAC:
        return [HOME / "Library" / "Application Support"]
    return [HOME / ".config", pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))]


def editor_workspace_roots() -> dict[str, list[pathlib.Path]]:
    out: dict[str, list[pathlib.Path]] = {}
    for tool, folder in (("Cursor", "Cursor"), ("Windsurf", "Windsurf"), ("VS Code", "Code")):
        out[tool] = [b / folder / "User" / "workspaceStorage" for b in app_support()]
    return out


def claude_home() -> pathlib.Path:
    """Claude Code's state directory. The CLI and the VS Code extension both
    honour CLAUDE_CONFIG_DIR and otherwise use ~/.claude."""
    return pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")


def codex_home() -> pathlib.Path:
    """Codex's state directory. The CLI, the desktop app and the VS Code
    extension all honour CODEX_HOME and otherwise use ~/.codex."""
    return pathlib.Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def cli_history_roots() -> dict[str, pathlib.Path]:
    """Agents that keep their own transcripts. The editor extensions of Claude
    Code and Codex write into these same trees, so they are covered as well.
    Codex moves a thread to archived_sessions/ when the user archives it."""
    return {
        "Claude Code": claude_home() / "projects",
        "Codex": codex_home() / "sessions",
        "Codex archive": codex_home() / "archived_sessions",
        "Gemini CLI": HOME / ".gemini",
    }


def claude_project_dirname(repo: pathlib.Path) -> str:
    """Claude Code names a project directory after the resolved absolute path
    with every character that is not a letter or digit replaced by a hyphen.
    Verified against real directories: dots, slashes and underscores all become
    hyphens. Collection does not rely on this alone; see agents/claude_code.py
    collect()."""
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))


def notify(message: str) -> None:
    """Best effort, never raises, never blocks for long."""
    import subprocess
    try:
        if IS_MAC:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{message}" with title "Interview"'],
                timeout=5, capture_output=True)
        elif IS_WIN:
            ps = ('[void][Windows.UI.Notifications.ToastNotificationManager,'
                  'Windows.UI.Notifications,ContentType=WindowsRuntime];'
                  '$t=[Windows.UI.Notifications.ToastNotificationManager]::'
                  'GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);'
                  f'$t.GetElementsByTagName("text").Item(0).AppendChild('
                  f'$t.CreateTextNode("Interview"))>$null;'
                  f'$t.GetElementsByTagName("text").Item(1).AppendChild('
                  f'$t.CreateTextNode("{message}"))>$null;'
                  '[Windows.UI.Notifications.ToastNotificationManager]::'
                  'CreateToastNotifier("Interview").Show('
                  '[Windows.UI.Notifications.ToastNotification]::new($t))')
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=10, capture_output=True)
        else:
            subprocess.run(["notify-send", "Interview", message],
                           timeout=5, capture_output=True)
    except Exception:
        # No notifier, or a headless session. The bell below still fires.
        pass
    # A bell always works, including over ssh and in a bare terminal.
    try:
        sys.stderr.write("\a")
        sys.stderr.flush()
    except Exception:
        # stderr closed or redirected somewhere that rejects writes. Nothing
        # about a notification may ever interrupt the recorder.
        pass
