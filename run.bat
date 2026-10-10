@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

echo ============================================
echo    超长文本翻译  -  启动中
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 goto :nopy

python -c "import fastapi, uvicorn, httpx" >nul 2>&1
if errorlevel 1 goto :deps

goto :serve

:nopy
echo [x] 没有找到 python 命令。
echo     请先安装 Python 3.10 以上版本，安装时勾选 "Add to PATH"。
echo.
pause
exit /b 1

:deps
echo [.] 首次运行，正在安装依赖（需要联网）...
python -m pip install -r requirements.txt
if errorlevel 1 goto :depfail
echo.
goto :serve

:depfail
echo [x] 依赖安装失败，请检查网络后重试。
echo     手动安装：pip install -r requirements.txt
echo.
pause
exit /b 1

:serve
set PORT=8765
python -c "import socket,sys;s=socket.socket();s.settimeout(1);r=s.connect_ex(('127.0.0.1',8765));s.close();sys.exit(0 if r else 1)" >nul 2>&1
if errorlevel 1 (
  set PORT=8766
  echo [!] 端口 8765 被占用，改用 8766
)

echo [.] 服务地址： http://127.0.0.1:%PORT%/
echo [.] 浏览器将自动打开，按 Ctrl+C 停止服务。
echo.
start "" "http://127.0.0.1:%PORT%/"

python -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level warning

echo.
echo 服务已停止。
pause