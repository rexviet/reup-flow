@echo off
rem Bam dup vao file nay tren may Windows render: tu lay code moi, cai goi, roi chay lai worker.
rem Chu khong dau: cua so cmd tren may nay khong hien duoc chu co dau.
setlocal
set "REPO=%~dp0"

rem git pull co the thay chinh file nay trong luc cmd dang doc no: chay tu ban sao trong TEMP.
if /i not "%~1"=="--from-temp" (
  copy /y "%~f0" "%TEMP%\cap-nhat-may-render.bat" >nul
  "%TEMP%\cap-nhat-may-render.bat" --from-temp "%REPO%"
  exit /b
)
set "REPO=%~2"
cd /d "%REPO%"
title Cap nhat may render

echo ============================================
echo   CAP NHAT VA CHAY LAI MAY RENDER
echo   Thu muc: %CD%
echo ============================================
echo.

for %%T in (git uv pnpm) do (
  where %%T >nul 2>&1 || (
    echo [LOI] May nay chua co "%%T" hoac chua nam trong PATH.
    echo       Xem docs\windows-render-worker.md, muc 5.1, de cai.
    goto :fail
  )
)

set "TASK=reup-render-worker"
schtasks /Query /TN %TASK% >nul 2>&1 && set HAS_TASK=1

echo [1/4] Dung worker dang chay...
if defined HAS_TASK schtasks /End /TN %TASK% >nul 2>&1
call :kill_worker
timeout /t 3 /nobreak >nul

echo [2/4] Lay code moi...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [LOI] Khong lay duoc code moi. Thuong la do tren may nay co file da bi sua tay.
  echo       Chup man hinh cua so nay gui nguoi phu trach.
  goto :start
)

echo [3/4] Cai goi Python va Node...
pushd services\localization
uv sync --frozen --no-dev -q
if errorlevel 1 (popd & echo [LOI] Cai goi Python khong duoc. & goto :start)
pushd spikes\hyperframes-review
rem O E: la exFAT, khong co symlink: cai kieu "hoisted" (chay duoc tren ca NTFS).
call pnpm install --frozen-lockfile --config.node-linker=hoisted --reporter=silent
if errorlevel 1 (popd & popd & echo [LOI] Cai goi Node khong duoc. & goto :start)
popd
popd

:start
echo [4/4] Chay lai worker...
if defined HAS_TASK (
  schtasks /Run /TN %TASK% >nul
) else (
  start "reup-render-worker" powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%REPO%scripts\render-worker.ps1"
)

echo       Doi worker khoi dong (toi da 60 giay)...
for /l %%i in (1,1,12) do (
  timeout /t 5 /nobreak >nul
  call :worker_running && goto :running
)

echo.
echo [LOI] Worker KHONG chay duoc. Chay worker ngay trong cua so nay de thay loi:
echo       chup man hinh phan chu ben duoi gui nguoi phu trach.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO%scripts\render-worker.ps1"
goto :fail

:running
for /f "delims=" %%C in ('git log -1 --format^="%%h  %%s"') do set "VERSION=%%C"
echo.
echo XONG. Worker dang chay, ban: %VERSION%
echo Dashboard se thay may nay song lai trong khoang 1 phut.
echo.
pause
exit /b 0

:fail
echo.
echo CHUA XONG. Bam mot phim de dong cua so.
pause
exit /b 1

:worker_running
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*localization.review_worker*' }) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

:kill_worker
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*localization.review_worker*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
exit /b 0
