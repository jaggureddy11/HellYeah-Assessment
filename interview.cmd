@echo off
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
