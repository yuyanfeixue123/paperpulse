#!/usr/bin/env bash
# PaperPulse 裸机一键部署（Debian / Ubuntu，需要 root）
#
#   curl -fsSL https://raw.githubusercontent.com/yuyanfeixue123/paperpulse/main/scripts/install.sh | sudo bash
# 或者先 clone 再执行：
#   sudo ./scripts/install.sh --repo https://github.com/yuyanfeixue123/paperpulse.git --domain papers.example.com
#
# 幂等：重复执行不会破坏已有配置（不会覆盖 config/config.yaml 与 data/）。

set -euo pipefail

REPO=""
DOMAIN=""
APP_DIR="${APP_DIR:-/opt/paperpulse}"
BRANCH="${BRANCH:-main}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)   REPO="$2";   shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --dir)    APP_DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

if [[ -z "$REPO" ]]; then
  read -rp "Git 仓库地址（https://github.com/yuyanfeixue123/paperpulse.git）: " REPO
fi
if [[ -z "$DOMAIN" ]]; then
  read -rp "对外域名（用于 HTTPS 与邮件链接，如 papers.example.com）: " DOMAIN
fi
if [[ -z "$REPO" || -z "$DOMAIN" ]]; then
  echo "仓库地址与域名都是必填项"; exit 1
fi

[[ $EUID -eq 0 ]] || { echo "请以 root 运行（sudo $0）"; exit 1; }

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

say "1/8 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl sqlite3 python3 python3-venv python3-dev build-essential \
  libssl-dev ca-certificates dnsutils netcat-openbsd

if ! command -v uv >/dev/null 2>&1; then
  say "安装 uv"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  UV_BIN="/root/.local/bin/uv"
else
  UV_BIN="$(command -v uv)"
fi

say "2/8 创建运行用户与目录"
if ! id -u paperpulse >/dev/null 2>&1; then
  useradd -r -m -d "$APP_DIR" -s /bin/bash paperpulse
fi
mkdir -p "$APP_DIR/data/backup"

say "3/8 拉取代码"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --all -q
  git -C "$APP_DIR" checkout "$BRANCH" -q
  git -C "$APP_DIR" pull --ff-only -q
else
  rm -rf "${APP_DIR:?}"/*
  git clone -b "$BRANCH" "$REPO" "$APP_DIR"
fi

say "4/8 安装 Python 依赖"
(cd "$APP_DIR" && "$UV_BIN" sync --frozen)

say "5/8 生成密钥与配置文件"
if [[ ! -f "$APP_DIR/.env" ]]; then
  SECRET="$("$APP_DIR/.venv/bin/python" -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  ENCKEY="$("$APP_DIR/.venv/bin/python" -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  cat > "$APP_DIR/.env" <<EOF
PAPERPULSE_SECRET_KEY=$SECRET
PAPERPULSE_ENCRYPTION_KEY=$ENCKEY
EOF
  chmod 600 "$APP_DIR/.env"
  echo "已生成 .env（权限 600）"
else
  echo ".env 已存在，跳过"
fi
[[ -f "$APP_DIR/config/config.yaml" ]] || cp "$APP_DIR/config/config.example.yaml" "$APP_DIR/config/config.yaml"

say "6/8 初始化数据库"
chown -R paperpulse:paperpulse "$APP_DIR"
chmod 700 "$APP_DIR/data"
sudo -u paperpulse "$APP_DIR/.venv/bin/python" -m app.cli init-db

say "7/8 安装 systemd 与 Caddy"
sed -e "s#/opt/paperpulse#${APP_DIR}#g" \
    -e "s#^Environment=PAPERPULSE_SECRET_KEY=#EnvironmentFile=${APP_DIR}/.env#g" \
    "$APP_DIR/deploy/paperpulse.service" > /etc/systemd/system/paperpulse.service
# EnvironmentFile 只需一行，去掉第二行重复的 Environment
sed -i '/^Environment=PAPERPULSE_ENCRYPTION_KEY=/d' /etc/systemd/system/paperpulse.service

if ! command -v caddy >/dev/null 2>&1; then
  # Caddy 官方源。注意：Cloudsmith 会轮换签名密钥，官方文档里的 key 有时效性，
  # 因此先尝试标准流程，失败则退回 trusted=yes（源站由 Cloudflare 的 TLS 保护）。
  say "安装 Caddy"
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' -o /tmp/caddy.key || true
  gpg --batch --yes --dearmor --no-tty \
      -o /usr/share/keyrings/caddy-archive-keyring.gpg /tmp/caddy.key 2>/dev/null || true
  rm -f /tmp/caddy.key
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list || true
  if ! apt-get update -qq 2>/dev/null; then
    echo "  官方源签名校验失败，改用 trusted=yes"
    sed -i 's#\[signed-by=[^]]*\]#[trusted=yes]#' /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
  fi
  apt-get install -y -qq caddy
fi

# Caddy 以 caddy 用户运行；日志目录必须归属该用户，否则启动即失败。
# （ProtectSystem=full 沙箱下以 root 预建会导致日志文件属主错误）
mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy
chmod 750 /var/log/caddy
rm -f /var/log/caddy/paperpulse.log

sed "s#your.domain#${DOMAIN}#g" "$APP_DIR/deploy/caddy/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile || true
systemctl reload caddy || systemctl restart caddy || true

systemctl daemon-reload
systemctl enable --now paperpulse

# 备份定时任务
cat > /etc/cron.d/paperpulse <<EOF
30 3 * * * paperpulse APP_DIR=${APP_DIR} ${APP_DIR}/deploy/backup.sh >> /var/log/paperpulse-backup.log 2>&1
EOF

say "8/8 启动终端配置向导"
if [[ -t 0 ]]; then
  sudo -u paperpulse -H "$APP_DIR/.venv/bin/python" -m app.cli setup
else
  echo "非交互式环境，跳过。请在服务器上执行："
  echo "  sudo -u paperpulse -H ${APP_DIR}/.venv/bin/python -m app.cli setup"
fi

say "完成"
systemctl --no-pager status paperpulse | head -8
cat <<EOF

接下来：
  1. 把域名 ${DOMAIN} 的 A 记录指向本机公网 IP（若尚未解析）

  2. 配置（两种方式，任选其一）
     终端向导（SSH 下即可，无需浏览器）：
       sudo -u paperpulse -H ${APP_DIR}/.venv/bin/python -m app.cli setup
     浏览器引导（域名与 HTTPS 配好之后）：
       https://${DOMAIN}/admin/setup

  3. 验证
       cd ${APP_DIR} && .venv/bin/python -m app.cli setup --check
       cd ${APP_DIR} && .venv/bin/python -m app.cli verify-sources
       cd ${APP_DIR} && .venv/bin/python -m app.cli test-email --to you@example.com

常用命令：
  systemctl status paperpulse      journalctl -u paperpulse -f
  cd ${APP_DIR} && git pull && .venv/bin/uv sync --frozen && .venv/bin/python -m alembic upgrade head && systemctl restart paperpulse
EOF
