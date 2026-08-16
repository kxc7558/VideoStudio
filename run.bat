@echo off
title 出片台 - 一键启动
cd /d "D:\VideoStudio"

echo ============================================
echo   出片台 一键启动
echo ============================================
echo.

rem ---------- 第 1 步：生成引擎（ComfyUI，端口 8188）----------
curl -s -o nul -m 3 http://127.0.0.1:8188/system_stats
if not errorlevel 1 goto comfy_ok
echo [1/2] 正在启动生成引擎（首次约 1-2 分钟，请耐心等）...
start "ComfyUI-引擎" cmd /c "cd /d D:\ComfyUI_Wan && venv\Scripts\python.exe main.py --lowvram --reserve-vram 1.0 --listen 127.0.0.1 --port 8188"

:wait_comfy
timeout /t 5 /nobreak >nul
curl -s -o nul -m 3 http://127.0.0.1:8188/system_stats
if not errorlevel 1 goto comfy_ok
echo [等待] 引擎还在启动，请勿关闭那个黑色窗口...
goto wait_comfy

:comfy_ok
echo [OK] 生成引擎已就绪

rem ---------- 第 2 步：出片台后端（端口 8000）----------
curl -s -o nul -m 3 http://127.0.0.1:8000/
if not errorlevel 1 goto client_ok
echo [2/2] 正在启动出片台...
start "出片台-后端" cmd /c "cd /d D:\VideoStudio && venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000"

set /a n=0
:wait_client
timeout /t 2 /nobreak >nul
curl -s -o nul -m 3 http://127.0.0.1:8000/
if not errorlevel 1 goto client_ok
set /a n+=1
if %n% geq 30 goto client_fail
goto wait_client

:client_fail
echo.
echo [失败] 出片台 60 秒内没启动成功，多半是程序报错了。
echo        请看标题为「出片台-后端」的黑色窗口，把里面的红色报错
echo        截图发给帮你维护的人。
echo.
pause
exit /b 1

:client_ok
echo [OK] 出片台已就绪，正在打开浏览器...
start http://127.0.0.1:8000

echo.
echo 全部启动完成，可以开始用了。
echo 用完关闭「ComfyUI-引擎」和「出片台-后端」两个窗口即可停止。
echo.
pause
