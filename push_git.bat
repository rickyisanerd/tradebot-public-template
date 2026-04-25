@echo off
setlocal

where git >nul 2>nul
if errorlevel 1 (
    echo Git is not installed or not on PATH.
    exit /b 1
)

if not exist .git (
    echo This folder is not a Git repository.
    exit /b 1
)

set "MESSAGE=%~1"
if "%MESSAGE%"=="" set "MESSAGE=Update %DATE% %TIME%"

set "BRANCH=%~2"
if "%BRANCH%"=="" (
    for /f %%i in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%i"
)

set "REMOTE=%~3"
if "%REMOTE%"=="" set "REMOTE=origin"

echo Staging changes...
git add -A
if errorlevel 1 exit /b 1

git diff --cached --quiet
if errorlevel 1 (
    echo Creating commit...
    git commit -m "%MESSAGE%"
    if errorlevel 1 exit /b 1
) else (
    echo No staged changes to commit. Skipping commit step.
)

echo Pushing to %REMOTE%/%BRANCH%...
git push -u %REMOTE% %BRANCH%
if errorlevel 1 exit /b 1

echo Push complete.