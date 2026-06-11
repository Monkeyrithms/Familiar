@echo off
REM Launch Familiar. Ensures Python prerequisites are installed first, then
REM starts the app without an IDE (pythonw = no console window).
cd /d "%~dp0"

REM Give the launcher an icon: (re)create Agent.lnk next to this file, pointing
REM at START.bat with assets\agent.ico. A .bat can't hold its own icon, so the
REM shortcut carries it. Also re-points to THIS folder each launch, so a .lnk
REM copied from another PC (stale absolute paths) self-heals. Silent; never gates launch.
set "FAMILIAR_ROOT=%~dp0"
REM Icon: prefer data\agent.ico (regenerated to the user's accent on each run),
REM fall back to the committed neutral assets\agent.ico (what ships in the package).
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$r=$env:FAMILIAR_ROOT.TrimEnd('\'); $t=Join-Path $r 'START.bat'; if(Test-Path -LiteralPath $t){$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut((Join-Path $r 'Agent.lnk')); $s.TargetPath=$t; $s.WorkingDirectory=$r; $s.WindowStyle=7; $s.Description='Familiar'; $id=Join-Path $r 'data\agent.ico'; $ia=Join-Path $r 'assets\agent.ico'; if(Test-Path -LiteralPath $id){$s.IconLocation=($id+',0')} elseif(Test-Path -LiteralPath $ia){$s.IconLocation=($ia+',0')}; $s.Save()}" >nul 2>&1

REM ── Find a WORKING Python 3.10+ ─────────────────────────────────────────
REM We don't just trust `where python`: on a fresh Windows machine `python`
REM is usually the Microsoft Store APP-EXECUTION-ALIAS STUB — it's on PATH but
REM only opens the Store and exits, so the old check "found" Python, the deps
REM install never ran, and the app launched into nothing. So we actually RUN
REM each candidate and confirm it's a real >=3.10 interpreter.
set "PYEXE="
py -3 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE python3 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1 && set "PYEXE=python3"

if not defined PYEXE (
    echo.
    echo [Familiar] Python 3.10 or newer was not found on this machine.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" in the installer, then run START.bat again.
    echo.
    echo Note: the "python" that pops open the Microsoft Store is NOT a real
    echo install -- use the python.org installer.
    echo.
    pause
    exit /b 1
)

REM ── Prerequisite gate ──────────────────────────────────────────────────
REM Fast no-op when deps are present; auto-installs them on a fresh machine.
REM Blocks launch (so you never run half-broken) if the install fails.
%PYEXE% "%~dp0core\bootstrap_deps.py"
if errorlevel 1 (
    echo.
    echo [Familiar] Required dependencies are missing and could not be installed.
    echo Fix the pip errors above, then run START.bat again.
    pause
    exit /b 1
)

REM ── Launch (no console window) ──────────────────────────────────────────
REM Derive the windowless launcher from the interpreter we confirmed works.
set "PYWEXE="
if "%PYEXE%"=="py -3" pyw -3 -c "import sys" >nul 2>&1 && set "PYWEXE=pyw -3"
if not defined PYWEXE pythonw -c "import sys" >nul 2>&1 && set "PYWEXE=pythonw"

if defined PYWEXE (
    start "" %PYWEXE% "%~dp0main.py"
    exit /b 0
)

REM Fallback: no windowless interpreter — run attached to THIS console so a
REM startup error is visible instead of the window just vanishing.
%PYEXE% "%~dp0main.py"
if errorlevel 1 pause
