@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 (
    echo 创建虚拟环境失败，请确认已安装 Python 并加入 PATH。
    pause
    exit /b 1
  )
  echo [2/3] 安装依赖...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
  echo 虚拟环境已存在，跳过安装。若依赖有更新请手动执行:
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo.
echo 默认账号:
echo   管理员  admin / admin123
echo   只读    viewer / view123
echo.
echo 启动后浏览器打开: http://127.0.0.1:8787
echo 局域网同事可访问: http://本机IP:8787
echo 按 Ctrl+C 可停止服务
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8787 --reload
pause
