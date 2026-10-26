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
# 设计开发阶段免登录：这个脚本启动的是本机演示站，登录只会挡自己。它只决定「我是谁」，
# 权限、部门范围、审计一条不少，页面右下角会一直显示「开发模式 · 免登录」角标。
# 恢复登录：WORKHUB_DEV_AUTOLOGIN=0 ./scripts/start-local-trial.sh
export WORKHUB_DEV_AUTOLOGIN="${WORKHUB_DEV_AUTOLOGIN:-1}"
echo "网站即将启动： http://127.0.0.1:8000"
echo "按 Ctrl+C 停止网站（数据库继续运行）。"
exec .venv/bin/python -m uvicorn workhub.api:app --host 127.0.0.1 --port 8000
