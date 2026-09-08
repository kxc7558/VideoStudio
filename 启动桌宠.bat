@echo off
rem 启动/重启 视频桌宠（3D 橘猫，贴边隐藏 + 暂停/继续/结束控制）
cd /d D:\VideoStudio
rem wmic 在 Win11 24H2+ 已移除，改用 CIM 查杀；注意进程是 pythonw 不是 python
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'pythonw.exe' -and $_.CommandLine -like '*_pet.py*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
start "" "D:\VideoStudio\venv\Scripts\pythonw.exe" "D:\VideoStudio\_pet.py"
echo 桌宠已启动（缩到屏幕角落找它）
