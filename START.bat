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

REM ── Prerequisite gate ──────────────────────────────────────────────────
REM Fast no-op when deps are present; auto-installs them on a fresh machine.
REM Blocks launch (so you never run half-broken) if the install fails.
set "PYEXE=py -3"
where py >nul 2>&1 || set "PYEXE=python"
%PYEXE% "%~dp0core\bootstrap_deps.py"
if errorlevel 1 (
    echo.
    echo [Familiar] Required dependencies are missing and could not be installed.
    echo Fix the pip errors above, then run START.bat again.
    pause
    exit /b 1
)

REM ── Launch (no console) ────────────────────────────────────────────────
where pyw >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" pyw -3 "%~dp0main.py"
    exit /b 0
)

where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)

REM Fallback: visible console for debugging if pythonw is missing.
py -3 "%~dp0main.py"
