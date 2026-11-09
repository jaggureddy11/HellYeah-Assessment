"""The two launcher scripts written into a kit.

The candidate types `./interview <command>` on macOS and Linux, or
`interview <command>` on Windows. Neither has a `.py` extension, so neither can
shadow a package name, and neither asks the candidate to know or type an interpreter.

Each one finds a usable Python itself and says something useful if it cannot.
"""

POSIX = r'''#!/bin/sh
# Interview session tooling. Finds a Python and gets out of the way.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for c in python3 python python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1; then PY=$c; break; fi
done

if [ -z "${PY:-}" ] && command -v uv >/dev/null 2>&1; then
  exec uv run --python 3.12 --no-project \
       "$HERE/.interview/ivkit/_entry.py" "$@"
fi

if [ -z "${PY:-}" ]; then
  echo "This needs a Python interpreter and cannot find one."
  echo
  echo "  macOS   xcode-select --install     (the same package that gives you git)"
  echo "  Linux   your package manager, e.g. apt install python3"
  echo "  either  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo
  echo "Tell us if this is a problem and we will send you a standalone build."
  exit 1
fi

exec "$PY" "$HERE/.interview/ivkit/_entry.py" "$@"
'''

WINDOWS = r'''@echo off
rem Interview session tooling. Finds a Python and gets out of the way.
setlocal
set HERE=%~dp0

where py >nul 2>nul && (
  py -3 "%HERE%.interview\ivkit\_entry.py" %*
  exit /b %errorlevel%
)
where python >nul 2>nul && (
  python "%HERE%.interview\ivkit\_entry.py" %*
  exit /b %errorlevel%
)
where uv >nul 2>nul && (
  uv run --python 3.12 --no-project "%HERE%.interview\ivkit\_entry.py" %*
  exit /b %errorlevel%
)

echo This needs a Python interpreter and cannot find one.
echo.
echo   winget install Python.Python.3.12
echo   or install uv: https://astral.sh/uv
echo.
echo Tell us if this is a problem and we will send you a standalone build.
exit /b 1
'''

ENTRY = r'''#!/usr/bin/env python3
"""Real entry point. The launchers call this; nobody needs to type it."""
import os, sys, pathlib

pkg_parent = pathlib.Path(__file__).resolve().parents[1]      # .interview
sys.path.insert(0, str(pkg_parent))
os.environ.setdefault("INTERVIEW_REPO", str(pkg_parent.parent))

from ivkit.cli import main
sys.exit(main(sys.argv[1:]))
'''
