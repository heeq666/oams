#!/usr/bin/env bash
# 启动 Workhub 本地试用网站（127.0.0.1:8000）。
# 数据库容器 workhub-live 需已在运行；数据保存在容器卷里，重启不丢。
set -eu

cd "$(dirname "$0")/.."

if ! docker ps --format '{{.Names}}' | grep -q '^workhub-live$'; then
  echo "数据库容器未运行，正在启动…"
  docker start workhub-live
  sleep 3
fi

export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@127.0.0.1:55460/workhub
echo "网站即将启动： http://127.0.0.1:8000"
echo "按 Ctrl+C 停止网站（数据库继续运行）。"
exec .venv/bin/python -m uvicorn workhub.api:app --host 127.0.0.1 --port 8000
