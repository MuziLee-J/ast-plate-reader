@echo off
chcp 65001 >nul
title 药敏板智能判读系统（本机）
cd /d "%~dp0"
echo.
echo  ============================================
echo   药敏板智能判读系统 —— 本机模式
echo   网址: http://127.0.0.1:8765
echo   （保持本窗口开启；关闭窗口即停止服务）
echo  ============================================
echo.
start "" cmd /c "timeout /t 6 >nul && start http://127.0.0.1:8765"
uv run --with fastapi --with uvicorn --with opencv-python --with numpy --with openpyxl --with pillow --with python-multipart python app.py
echo.
echo  服务已停止。按任意键关闭窗口。
pause >nul
