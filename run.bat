@echo off
title 出片台 - 一键启动
cd /d "D:\VideoStudio"

echo ============================================
echo   出片台 一键启动
echo ============================================
echo.

curl -s -o nul -m 3 http://127.0.0.1:8188/system_stats
if errorlevel 1 goto start_comfy
echo [OK] 生成引擎已在运行
goto start_client

:start_comfy
echo [启动] 正在启动生成引擎（首次约需 1-2 分钟加载模型）...
start "ComfyUI-引擎" cmd /c "cd /d D:\ComfyUI_Wan && venv\Scripts\python.exe main.py --lowvram --reserve-vram 1.0 --listen 127.0.0.1 --port 8188"
echo [等待] 等待引擎就绪...
:wait_comfy
timeout /t 5 /nobreak >nul
curl -s -o nul -m 3 http://127.0.0.1:8188/system_stats
if errorlevel 1 goto wait_comfy
echo [OK] 引擎就绪

:start_client
echo [启动] 启动出片台服务...
start "出片台-服务" cmd /c "cd /d D:\VideoStudio && venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000"

timeout /t 3 /nobreak >nul
start http://127.0.0.1:8000

echo.
echo 全部就绪！浏览器里就能用了。
echo 关掉「ComfyUI-引擎」和「出片台-服务」两个窗口即可停止。
echo.
pause
