#!/usr/bin/env bash
# 每日 03:30 执行：SQLite 在线备份（VACUUM INTO 不阻塞读写），保留最近 7 份。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/paperpulse}"
BACKUP_DIR="${APP_DIR}/data/backup"
DB="${APP_DIR}/data/paperpulse.db"
KEEP="${KEEP:-7}"

mkdir -p "${BACKUP_DIR}"

if [ ! -f "${DB}" ]; then
  echo "数据库不存在：${DB}" >&2
  exit 1
fi

TARGET="${BACKUP_DIR}/paperpulse-$(date +%F).db"
sqlite3 "${DB}" "VACUUM INTO '${TARGET}'"

# 保留最近 KEEP 份
ls -1t "${BACKUP_DIR}"/paperpulse-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "备份完成：${TARGET}"
