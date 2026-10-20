@echo off
chcp 65001 >nul
title 药敏板智能判读系统（局域网）
cd /d "%~dp0"
set AST_HOST=0.0.0.0
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set LOCALIP=%%a
    goto :found
)
:found
set LOCALIP=%LOCALIP: =%
echo.
echo  ============================================
echo   药敏板智能判读系统 —— 局域网模式
echo   本机访问:     http://127.0.0.1:8765
echo   内网访问:     http://%LOCALIP%:8765
echo   （手机/同网段电脑用"内网访问"地址打开；
echo     首次运行如弹出防火墙提示，请勾选"专用网络"并允许）
echo   保持本窗口开启；关闭窗口即停止服务
echo  ============================================
echo.
start "" cmd /c "timeout /t 6 >nul && start http://127.0.0.1:8765"
uv run --with fastapi --with uvicorn --with opencv-python --with numpy --with openpyxl --with pillow --with python-multipart python app.py
echo.
echo  服务已停止。按任意键关闭窗口。
pause >nul
