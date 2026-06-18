@echo off
chcp 65001 >nul
setlocal

echo ==========================================
echo   QQ AI Bot 环境初始化（Windows）
echo ==========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9 或更高版本。
    echo https://www.python.org/downloads/
    exit /b 1
)

echo [1/3] 创建虚拟环境...
python -m venv venv
if errorlevel 1 exit /b 1

echo [2/3] 安装依赖...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo [3/3] 准备配置文件...
if not exist .env copy .env.example .env >nul

echo.
echo 初始化完成。请编辑 .env，然后运行 start.bat。
echo NapCat 反向 WebSocket: ws://127.0.0.1:8080/onebot/v11/ws
