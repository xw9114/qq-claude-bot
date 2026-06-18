#!/usr/bin/env bash
set -euo pipefail

echo "=========================================="
echo "  QQ AI Bot 环境初始化（Linux / macOS）"
echo "=========================================="

if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：未找到 Python 3，请先安装 Python 3.9 或更高版本。" >&2
    exit 1
fi

echo "[1/3] 创建虚拟环境..."
python3 -m venv venv

echo "[2/3] 安装依赖..."
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[3/3] 准备配置文件..."
if [[ ! -f .env ]]; then
    cp .env.example .env
fi

echo
echo "初始化完成。请编辑 .env，然后运行 ./start.sh。"
echo "NapCat 反向 WebSocket: ws://127.0.0.1:8080/onebot/v11/ws"
