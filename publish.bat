@echo off
setlocal
cd /d "%~dp0"
where git >nul 2>nul || (echo Git is not installed. Install from https://git-scm.com and rerun. & pause & exit /b 1)
where gh >nul 2>nul || (echo GitHub CLI missing. Run:  winget install GitHub.cli   then rerun. & pause & exit /b 1)
gh auth status >nul 2>nul || gh auth login
if not exist .git git init -b main
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git add -A
  git commit -m "Orbit: prototype, build plan, research" || echo Nothing new to commit.
  gh repo create 20Nick00/orbit --public --source . --remote origin --push --description "Orbit - open-source, self-hosted wealth tracker"
  gh api repos/20Nick00/orbit/pages -X POST -f "source[branch]=main" -f "source[path]=/docs" >nul 2>nul
  echo.
  echo Repo created. Prototype will be live at https://20nick00.github.io/orbit/ in about a minute.
) else (
  git add -A
  git commit -m "Update %date% %time%" || echo Nothing new to commit.
  git push || (echo Push failed, so nothing was published. & pause & exit /b 1)
  gh api repos/20Nick00/orbit/pages -X POST -f "source[branch]=main" -f "source[path]=/docs" >nul 2>nul
  echo.
  echo Pushed. Latest: https://20nick00.github.io/orbit/
)
pause
