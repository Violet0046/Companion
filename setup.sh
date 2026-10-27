#!/usr/bin/env bash
# setup.sh — 初始化开发环境（venv + 依赖 + .env）
#
# 用法：
#   chmod +x setup.sh
#   ./setup.sh
#
# 幂等：重复运行不会破坏现有环境。

set -e

cd "$(dirname "$0")"

VENV_DIR=".venv"
PYTHON="${PYTHON:-python3}"

echo "==> Checking Python version..."
PY_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]); then
    echo "[FAIL] 需要 Python 3.9+（当前 $PY_VERSION）"
    exit 1
fi
echo "    OK (Python $PY_VERSION)"

# 1. 创建 venv（如果不存在）
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtualenv at $VENV_DIR ..."
    $PYTHON -m venv "$VENV_DIR"
else
    echo "    $VENV_DIR 已存在，跳过创建"
fi

# 2. 升级 pip + 安装依赖 + 可编辑安装本包
echo "==> Installing dependencies ..."
"$VENV_DIR/bin/pip" install --upgrade pip > /dev/null
"$VENV_DIR/bin/pip" install -r requirements.txt
"$VENV_DIR/bin/pip" install -e .

# 3. 复制 .env.example → .env（如果 .env 不存在）
if [ ! -f ".env" ]; then
    echo "==> Copying .env.example → .env ..."
    cp .env.example .env
    echo "    [TODO] 编辑 .env 填入真实 API key"
else
    echo "    .env 已存在，跳过复制"
fi

echo ""
echo "============================================="
echo "  Setup complete!"
echo "============================================="
echo ""
echo "下一步："
echo "  1. 编辑 .env 填入 OPENAI_API_KEY 等："
echo "       vim .env"
echo ""
echo "  2. 激活虚拟环境："
echo "       source .venv/bin/activate"
echo ""
echo "  3. 跑 CLI（package 已经被 editable install，不用设 PYTHONPATH）："
echo "       companion-cli setup --user alice --persona shushu --template hot_girlfriend"
echo "       companion-cli chat --user alice --persona shushu"
echo "       companion-cli show --user alice --persona shushu"
echo ""
echo "（或用 source .venv/bin/activate 后 python -m companion_ever.cli ... 也行）"
echo ""
echo "（不需要每次激活 venv——直接用 .venv/bin/python 也行）"
echo ""