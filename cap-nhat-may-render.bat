@echo off
rem Bam dup vao file nay tren may Windows render: tu lay code moi, cai goi, roi chay lai worker.
rem Cap nhat may render (render-worker-win). Khong can go lenh nao.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Cập nhật máy render

echo ============================================
echo   CẬP NHẬT VÀ CHẠY LẠI MÁY RENDER
echo   Thư mục: %CD%
echo ============================================
echo.

for %%T in (git uv pnpm) do (
  where %%T >nul 2>&1 || (
    echo [LỖI] Máy này chưa có "%%T" hoặc chưa nằm trong PATH.
    echo       Xem docs\windows-render-worker.md, mục 5.1, để cài.
    goto :fail
  )
)

set TASK=reup-render-worker
schtasks /Query /TN %TASK% >nul 2>&1 && set HAS_TASK=1

echo [1/4] Dừng worker đang chạy...
if defined HAS_TASK (
  schtasks /End /TN %TASK% >nul 2>&1
) else (
  taskkill /FI "WINDOWTITLE eq reup-render-worker*" /T /F >nul 2>&1
)
timeout /t 3 /nobreak >nul

echo [2/4] Lấy code mới...
git pull --ff-only || (
  echo.
  echo [LỖI] Không lấy được code mới. Thường là do trên máy này có file đã bị sửa tay.
  echo       Gửi ảnh cửa sổ này cho người phụ trách.
  goto :restart_old
)

echo [3/4] Cài gói Python...
pushd services\localization
uv sync --frozen --no-dev || (popd & echo [LỖI] Cài gói Python không được. & goto :restart_old)
echo       Cài gói Node cho HyperFrames...
pushd spikes\hyperframes-review
call pnpm install --frozen-lockfile || call pnpm install --frozen-lockfile --config.node-linker=hoisted || (popd & popd & echo [LỖI] Cài gói Node không được. & goto :restart_old)
popd
popd

:start
echo [4/4] Chạy lại worker...
if defined HAS_TASK (
  schtasks /Run /TN %TASK% >nul
) else (
  start "reup-render-worker" powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0scripts\render-worker.ps1"
)
echo.
for /f "delims=" %%C in ('git log -1 --format^="%%h  %%s"') do set VERSION=%%C
echo XONG. Máy render đang chạy bản: %VERSION%
echo Dashboard sẽ thấy máy này sống lại trong khoảng 1 phút.
echo.
pause
exit /b 0

:restart_old
echo.
echo Chạy lại worker bằng bản cũ để máy không đứng yên...
if defined HAS_TASK (schtasks /Run /TN %TASK% >nul) else (start "reup-render-worker" powershell -NoProfile -ExecutionPolicy Bypass -NoExit -File "%~dp0scripts\render-worker.ps1")

:fail
echo.
echo CHƯA CẬP NHẬT ĐƯỢC. Cửa sổ sẽ đóng khi bạn bấm một phím.
pause
exit /b 1
