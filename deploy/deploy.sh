#!/usr/bin/env bash
# 同行 —— 服务器一键部署
#
# 用法：
#   sudo bash deploy/deploy.sh /path/to/companion-ever-memory
#
# 做的事：
#   1. 装 Python 3.10+（项目下限 3.10：pydantic 用了 `str | None`）
#   2. 建 companion 系统用户
#   3. 代码放到 /opt/companion，建 venv，pip install -e .
#   4. 写 /etc/companion/companion.env（随机 PIN）
#   5. 装 systemd unit，开机自启
#   6. 起服务并自检
#
# 幂等：重复跑安全（会重装依赖、重启服务，不动数据库）。
set -euo pipefail

APP_DIR=/opt/companion
ENV_FILE=/etc/companion/companion.env
SERVICE_USER=companion

log()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "要用 root 跑：sudo bash deploy/deploy.sh <源码目录>"
SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
[ -f "$SRC_DIR/pyproject.toml" ] || die "找不到源码：$SRC_DIR"

# ---------- 1. Python ----------
log "装 Python"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip python3-devel >/dev/null
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 python3-pip python3-devel >/dev/null
else
    die "不认识的包管理器（apt/dnf/yum）"
fi

PY=""
for c in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)'; then
            PY="$c"; break
        fi
    fi
done
[ -n "$PY" ] || die "没找到 Python 3.10+（项目下限 3.10）"
echo "   用 $PY（$($PY --version)）"

# ---------- 2. 用户 ----------
log "建系统用户 $SERVICE_USER"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

# ---------- 3. 代码 + venv ----------
log "部署代码到 $APP_DIR"
mkdir -p "$APP_DIR"
if [ -d "$APP_DIR/.git" ] && [ -n "${REPO_URL:-}" ]; then
    echo "   git pull"
    git -C "$APP_DIR" pull --ff-only
else
    echo "   从 $SRC_DIR 同步（排除运行时产物）"
    rsync -a --delete \
        --exclude '.git' --exclude '.venv' --exclude 'memory' \
        --exclude '__pycache__' --exclude '.pytest_cache' \
        "$SRC_DIR"/ "$APP_DIR"/
fi

log "装依赖"
"$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -q --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/python" -m pip install -q -e "$APP_DIR"
echo "   $("$APP_DIR/.venv/bin/python" -c 'import companion_ever,sys; print("导入正常")')"

# ---------- 4. 环境变量 ----------
log "写 $ENV_FILE"
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
    PIN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
    sed -e "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=sk-改成你的key|" \
        -e "s|^COMPANION_WEB_PIN=.*|COMPANION_WEB_PIN=$PIN|" \
        "$SRC_DIR/deploy/.env.production" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "   已生成，PIN 是：$PIN"
    echo "   ⚠ 记得把 OPENAI_API_KEY / BASE_URL / MODEL 改对"
else
    echo "   已存在，保留不动"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ---------- 5. systemd ----------
log "装 systemd unit"
install -m 644 "$SRC_DIR/deploy/companion.service" /etc/systemd/system/companion.service
systemctl daemon-reload
systemctl enable companion >/dev/null 2>&1 || true

# ---------- 6. 起服务 ----------
log "启动"
systemctl restart companion
sleep 3
systemctl --no-pager --lines=15 status companion || true

log "自检"
HEALTH="$(curl -fsS --max-time 5 http://127.0.0.1:"$(grep -oP 'COMPANION_WEB_PORT=\K\d+' "$ENV_FILE" || echo 8000)"/api/health || true)"
if [ -n "$HEALTH" ]; then
    echo "   /api/health → $HEALTH"
else
    warn "健康检查没通过，看日志：journalctl -u companion -n 50"
fi

cat <<'EOF'

下一步：
  1. 改 /etc/companion/companion.env 里的模型配置（key / base_url / model）
     然后：systemctl restart companion
  2. 建第一个用户和角色：
     sudo -u companion $APP_DIR/.venv/bin/python -m companion_ever setup \
        --user alice --persona 小伴 --template hot_girlfriend
  3. 访问：
     · 有域名+证书：配好 nginx 后 https://你的域名
     · 还没备案：先用 http://<服务器IP>:8000（无 HTTPS，PWA 离线/推送不可用）
  4. 防火墙放行：80 / 443（或临时 8000）
EOF
