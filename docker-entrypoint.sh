#!/bin/sh
# Container entrypoint: make sure the SQLite directory exists and is writable,
# then hand over to the CMD (gunicorn).
set -eu

DB_PATH="${DATABASE_PATH:-/app/data/reports.db}"
DB_DIR="$(dirname "$DB_PATH")"

if ! mkdir -p "$DB_DIR" 2>/dev/null; then
    echo "启动失败：无法创建数据库目录 $DB_DIR" >&2
    exit 1
fi

if [ ! -w "$DB_DIR" ]; then
    echo "启动失败：数据库目录 $DB_DIR 不可写。" >&2
    echo "使用 bind mount 时请确认宿主机目录对容器用户 (uid 10001) 可写，" >&2
    echo "或改用命名卷（compose 默认已配置）。" >&2
    exit 1
fi

exec "$@"
