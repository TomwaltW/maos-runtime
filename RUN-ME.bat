@echo off
rem ===========================================================================
rem  RUN-ME.bat  --  Windows entry point (double-click to run)
rem ===========================================================================
rem
rem  Re-runs the whole evidence chain on your own machine and opens the report
rem  when it finishes. This step is OPTIONAL: if you do not have Python, just
rem  double-click START-HERE.html instead -- that is a static page, no Python
rem  and no network needed.
rem
rem  ---------------------------------------------------------------------
rem  WHY THIS FILE IS PURE ASCII while the rest of the project is Chinese
rem  ---------------------------------------------------------------------
rem  A .bat is parsed by cmd.exe line by line using the CURRENT code page,
rem  which is 936 (GBK) on a default Chinese Windows. This file is stored as
rem  UTF-8, so any Chinese byte in it would be mis-decoded -- and a mangled
rem  multi-byte sequence can swallow the following character, including the
rem  line terminator, which corrupts the NEXT command rather than merely
rem  printing garbage. This file is the only thing that can still talk to the
rem  user when Python is missing, so it must execute correctly under ANY code
rem  page. Its messages therefore stay ASCII.
rem
rem  The Chinese output lives one layer down, in client\preflight.py: Python
rem  3.6+ writes to the Windows console through the wide-char API, so Chinese
rem  renders correctly there regardless of the code page. The `chcp 65001`
rem  below is belt-and-braces for the cases that API does not cover (output
rem  redirected to a file or a pipe).
rem
rem  This file must be saved with CRLF line endings. LF-only .bat files
rem  misbehave on some Windows builds (labels and `goto` in particular).
rem ===========================================================================

chcp 65001 >nul 2>&1

setlocal

rem Double-clicking a .bat can leave the working directory somewhere else
rem (a network share, C:\Windows\System32 when run elevated), so switch to the
rem folder this file lives in. %~dp0 is that folder, with a trailing backslash;
rem /d also switches the drive letter. Quoted, because the path may contain
rem spaces.
cd /d "%~dp0"

if not exist "client\preflight.py" goto incomplete

rem Probe order py -3 -> python3 -> python, identical to RUN-ME.command.
rem `py` (the official launcher) goes first on purpose: on Windows 10/11 a bare
rem `python` is an App Execution Alias stub that opens the Microsoft Store when
rem Python is not installed, so we only ever reach it as a last resort.
rem Note there is no `>=` in the probe expression: cmd.exe treats > as a
rem redirection operator, so `max(a,b)==a` is used to compare the version tuple.
rem The expression is also Python 2 compatible, which means an ancient
rem interpreter fails the probe cleanly instead of raising SyntaxError.
set "PYBIN="
set "PYARGS="

call :probe py -3
call :probe python3
call :probe python

if not defined PYBIN goto nopython

"%PYBIN%" %PYARGS% "client\preflight.py" %*
set "RC=%ERRORLEVEL%"
goto done

rem ---------------------------------------------------------------------------
:probe
if defined PYBIN goto :eof
%* -c "import sys;raise SystemExit(0 if max(sys.version_info[:2],(3,10))==sys.version_info[:2] else 1)" >nul 2>&1
if errorlevel 1 goto :eof
set "PYBIN=%1"
set "PYARGS=%2"
goto :eof

rem ---------------------------------------------------------------------------
:incomplete
echo.
echo ================================================================
echo   This folder is incomplete
echo ================================================================
echo.
echo   client\preflight.py is missing. The zip was probably only
echo   partly extracted, or a few files were dragged out of it.
echo.
echo   Please extract the whole zip again, then double-click this
echo   file inside the extracted folder.
echo.
set "RC=2"
goto done

rem ---------------------------------------------------------------------------
:nopython
echo.
echo ================================================================
echo   Python 3.10 or newer was not found on this computer
echo ================================================================
echo.
echo   This step is OPTIONAL. It lets you re-run the evidence chain
echo   yourself, so you can confirm the numbers in the report were
echo   produced by the code and not typed in by us.
echo.
echo   You can see everything without Python:
echo       go back to this folder and double-click  START-HERE.html
echo   That is a static page. No Python, no network, no account.
echo.
echo   To run it yourself: install Python 3.10 or newer from
echo   python.org (tick "Add python.exe to PATH" in the installer),
echo   then double-click this file again.
echo.
set "RC=3"
goto done

rem ---------------------------------------------------------------------------
:done
echo.
echo Press any key to close this window...
pause >nul
endlocal & exit /b %RC%
