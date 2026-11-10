@echo off
chcp 65001 >nul
title FileServer
echo [INFO] 正在启动后端服务...

python file_search.py

echo [INFO] 服务退出
pause
