#!/usr/bin/env bash
# 启动 OAMS 本地试用网站（127.0.0.1:8000）。
# 数据库容器 oams-live 需已在运行；数据保存在容器卷里，重启不丢。
set -eu

cd "$(dirname "$0")/.."

if ! docker ps --format '{{.Names}}' | grep -q '^oams-live$'; then
  echo "数据库容器未运行，正在启动…"
  docker start oams-live
  sleep 3
fi

# 演示库连接串不写进仓库：优先环境变量，其次本机 tmp/demo-db-url（tmp/ 已被 git 忽略）。
# 换机器就把那一行连接串写进 tmp/demo-db-url，或者 export OAMS_DATABASE_URL 后再跑。
if [[ -z "${OAMS_DATABASE_URL:-}" && -f tmp/demo-db-url ]]; then
  OAMS_DATABASE_URL="$(tr -d '\n' < tmp/demo-db-url)"
fi
if [[ -z "${OAMS_DATABASE_URL:-}" ]]; then
  echo "缺少演示库连接串：export OAMS_DATABASE_URL=... 或写进 tmp/demo-db-url。" >&2
  exit 2
fi
export OAMS_DATABASE_URL
# 设计开发阶段免登录：这个脚本启动的是本机演示站，登录只会挡自己。它只决定「我是谁」，
# 权限、部门范围、审计一条不少，页面右下角会一直显示「开发模式 · 免登录」角标。
# 恢复登录：OAMS_DEV_AUTOLOGIN=0 ./scripts/start-local-trial.sh
export OAMS_DEV_AUTOLOGIN="${OAMS_DEV_AUTOLOGIN:-1}"
echo "网站即将启动： http://127.0.0.1:8000"
echo "按 Ctrl+C 停止网站（数据库继续运行）。"
exec .venv/bin/python -m uvicorn oams.api:app --host 127.0.0.1 --port 8000
