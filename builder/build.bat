@echo off
REM ==========================================================================
REM  ScriptDB - portable build
REM
REM  Creates builder\dist\ScriptDB.exe: one self-contained file that runs on a
REM  Windows machine with no Python and no installer. The temporary work folder
REM  (build\) is deleted again at the end, whether the build succeeded or not.
REM
REM  Usage:
REM      build.bat            full clean build
REM      build.bat /fast      keep the previous work folder (a little quicker)
REM      build.bat /nopause   do not wait for a key at the end (for CI)
REM ==========================================================================

setlocal EnableDelayedExpansion

REM ---------------------------------------------------------------- settings
set APP_NAME=ScriptDB
set APP_VERSION=2.5.0
set ENTRY=sdb_scripts.py

REM This file lives in builder\, the project is one level up.
set BUILDER_DIR=%~dp0
pushd "%BUILDER_DIR%.." || (echo Cannot enter the project folder. & exit /b 1)
set PROJECT_DIR=%CD%

REM %BUILDER_DIR% already ends with a backslash, so never add another one.
set BUILD_DIR=%BUILDER_DIR%build
set DIST_DIR=%BUILDER_DIR%dist
set SPEC_FILE=%BUILDER_DIR%%APP_NAME%.spec
set ICON_FILE=%BUILDER_DIR%sdb.ico
set EXE_PATH=%DIST_DIR%\%APP_NAME%.exe

set FAST=0
set NOPAUSE=0
for %%A in (%*) do (
    if /I "%%~A"=="/fast"    set FAST=1
    if /I "%%~A"=="/nopause" set NOPAUSE=1
)

echo ==========================================================================
echo  ScriptDB %APP_VERSION% - portable build
echo ==========================================================================
echo  Project : %PROJECT_DIR%
echo  Output  : %EXE_PATH%
echo  Mode    : %FAST% (1 = keep work folder, 0 = clean)
echo.

REM ------------------------------------------------------------ find Python
REM Several Python installs often coexist. The first interpreter that already
REM has PyInstaller wins, so a stray preview build without it is skipped.
set PYTHON=
call :try_python python
if not defined PYTHON call :try_python py
if not defined PYTHON (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
        call :try_python2 "%%~fD\python.exe"
    )
)
if not defined PYTHON (
    echo [ERROR] No Python interpreter with PyInstaller was found.
    echo         Install it with:  python -m pip install pyinstaller
    goto :fail
)
echo Python  : %PYTHON%
%PYTHON% --version

if not exist "%ENTRY%" (
    echo [ERROR] Entry script not found: %ENTRY%
    goto :fail
)

if not exist "%SPEC_FILE%" (
    echo [ERROR] Spec file not found: %SPEC_FILE%
    goto :fail
)

REM app_paths owns the application data folder. Without it in the bundle the exe
REM would fall back to writing next to itself, so the build stops here instead.
if not exist "%PROJECT_DIR%\app_paths.py" (
    echo [ERROR] app_paths.py not found: %PROJECT_DIR%\app_paths.py
    echo         It is required for the application data folder to work.
    goto :fail
)

if exist "%ICON_FILE%" (
    echo [OK]    Icon found: sdb.ico
) else (
    echo [WARN]  sdb.ico is missing - the exe will use the default icon.
)

echo.
echo [1/5] Closing a running %APP_NAME% and removing previous output...
REM Windows locks a running exe, so a rebuild would fail with "Access denied".
REM Only taskkill is used: tasklist, find and timeout are missing on some
REM minimal PATH setups, and a missing helper must never abort the build.
REM taskkill prints "not found" when nothing matches, which is harmless here.
echo       closing %APP_NAME%.exe if it is running ...
REM The close is done from Python, not with taskkill: taskkill (and tasklist,
REM find, timeout) are missing on some minimal PATH setups, and a missing
REM helper must never abort a build. Python is already required, so it is the
REM one tool guaranteed to be present.
set STOP_HELPER=%BUILDER_DIR%stop_running.py
%PYTHON% "%STOP_HELPER%" %APP_NAME% 20
if errorlevel 1 (
    echo [ERROR] %APP_NAME%.exe is still running and could not be closed.
    echo         Close it by hand and run the build again.
    goto :fail
)
if exist "%DIST_DIR%" rd /s /q "%DIST_DIR%" >nul 2>&1
if exist "%DIST_DIR%" (
    echo [ERROR] Could not clear %DIST_DIR%.
    echo         Close any program using %APP_NAME%.exe and try again.
    goto :fail
)
if "%FAST%"=="0" if exist "%BUILD_DIR%" rd /s /q "%BUILD_DIR%" 2>nul
mkdir "%DIST_DIR%" 2>nul
echo       done.

echo.
echo [2/5] Running static self-check (_scan.py)...
if exist "%PROJECT_DIR%\_scan.py" (
    pushd "%PROJECT_DIR%"
    %PYTHON% _scan.py
    popd
) else (
    echo       skipped - _scan.py not present.
)

echo.
echo [3/5] Compiling with PyInstaller...
echo       (this takes a minute or two)
REM PyInstaller refuses --paths when a .spec file is given; the import path is
REM set by pathex=[PROJECT_DIR] in the spec instead, so app_paths is found
REM during analysis.
set START_TIME=%TIME%
%PYTHON% -m PyInstaller --noconfirm --clean --distpath "%DIST_DIR%" --workpath "%BUILD_DIR%" "%SPEC_FILE%"
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller failed. Scroll up for the first traceback.
    goto :fail
)
set END_TIME=%TIME%
echo       done.

echo.
echo [4/5] Verifying the executable...
if not exist "%EXE_PATH%" (
    echo [ERROR] Expected %EXE_PATH% but it was not produced.
    goto :fail
)

for %%F in ("%EXE_PATH%") do (
    set EXE_SIZE=%%~zF
    set EXE_DATE=%%~tF
)
set /a EXE_MB=!EXE_SIZE! / 1048576
set /a EXE_KB=!EXE_SIZE! / 1024

echo       found: %APP_NAME%.exe

echo.
echo [5/5] Cleaning up the work folder...
if "%FAST%"=="0" (
    if exist "%BUILD_DIR%" rd /s /q "%BUILD_DIR%"
    echo       removed: build\
) else (
    echo       kept: build\ (because of /fast)
)
if exist "%PROJECT_DIR%\__pycache__"          rd /s /q "%PROJECT_DIR%\__pycache__"
if exist "%PROJECT_DIR%\tabs\__pycache__"     rd /s /q "%PROJECT_DIR%\tabs\__pycache__"
if exist "%PROJECT_DIR%\tabs\syntax\__pycache__" rd /s /q "%PROJECT_DIR%\tabs\syntax\__pycache__"
echo       removed: __pycache__ folders

REM ------------------------------------------------------------------ summary
echo.
echo ==========================================================================
echo  BUILD SUMMARY
echo ==========================================================================
echo  Status         : SUCCESS
echo  Application    : %APP_NAME% %APP_VERSION%
echo  Executable     : %EXE_PATH%
echo  Size           : !EXE_SIZE! bytes  (!EXE_MB! MB, !EXE_KB! KB)
echo  Built at       : !EXE_DATE!
echo  Started        : %START_TIME%   Finished: %END_TIME%
echo  Icon embedded   : %ICON_FILE%
echo  Mode           : portable, one file, no installer, no registry writes
echo  Requires Python: no
echo.
echo What is inside:
echo    - sdb_scripts.py, app_paths.py and the whole tabs\ package
echo    - all 13 syntax modules
echo    - the search engine and the navigation engine
echo    - sdb.ico, used for the window icon at run time
echo    - default copies of the databases, copied into the data folder on
echo      first run (never overwriting an existing file)
echo.
echo Where the data goes:
echo    %APPDATA%\.polsoft\ScriptDB         program files
echo    %APPDATA%\.polsoft\ScriptDB\db\      databases
echo    (the program works from any folder, so C:\Program Files is fine)
echo.
echo Distribute:
echo    Copy "%EXE_PATH%" anywhere and run it. Everything the program writes goes
echo    to %APPDATA%\.polsoft\ScriptDB\db (databases) and the folder above it
echo    (reports, exports, logs), so the exe needs no write access to its own
echo    folder.
echo.
echo  NOTE: unsigned executables often trigger SmartScreen. Users click
echo        "More info" -^> "Run anyway", or you sign the exe with signtool.
echo ==========================================================================
echo.

REM Everything below must not change the exit code: a successful build returns
REM 0 even when a helper (dir, popd, pause) is missing from PATH.
if exist "%EXE_PATH%" (
    echo Produced: %EXE_PATH%
)

cd /d "%USERPROFILE%" 2>nul
if "%NOPAUSE%"=="0" (
    echo.
    pause
)
endlocal
exit /b 0

REM --------------------------------------------------------- Python probes
REM A probe accepts an interpreter only when it can import PyInstaller, so a
REM second install that lacks it is passed over.
:try_python
if defined PYTHON goto :eof
where %~1 >nul 2>&1
if errorlevel 1 goto :eof
%~1 -c "import PyInstaller" >nul 2>&1
if errorlevel 1 goto :eof
set PYTHON=%~1
goto :eof
:try_python2
if defined PYTHON goto :eof
if not exist %~1 goto :eof
%~1 -c "import PyInstaller" >nul 2>&1
if errorlevel 1 goto :eof
set PYTHON=%~1
goto :eof
:fail
echo.
echo ==========================================================================
echo  BUILD SUMMARY
echo ==========================================================================
echo  Status : FAILED   (see the messages above)
echo  No executable was produced in %DIST_DIR%
echo ==========================================================================
if exist "%BUILD_DIR%" if "%FAST%"=="0" rd /s /q "%BUILD_DIR%"
popd
if "%NOPAUSE%"=="0" (
    echo.
    pause
)
endlocal
exit /b 1
