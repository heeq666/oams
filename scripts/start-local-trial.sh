#!/usr/bin/env bash
# 启动 OAMS 本地试用网站。
#
# **自备 PostgreSQL**：这个脚本不建库、不建表、不猜连接串，只把网站起起来。
# 库和表按 README 的「一条命令跑起来」准备（oams db init / oams bootstrap-admin）。
#
# 变量从上到下（都从这里来，脚本不会再摸别的文件替你兜底）：
#   OAMS_DATABASE_URL    必填。连接串，例：postgresql://<账号>:<口令>@127.0.0.1:5432/<库名>
#   OAMS_DB_CONTAINER    可选。库跑在 docker 里时写容器名，默认 oams-pg；
#                        没在运行就 docker start 它。库不是 docker 起的（本机装的、
#                        或外部机器上的）把这条留空即可，找不到容器时脚本会说明并继续。
#   OAMS_BIND_HOST       可选。监听地址，默认 127.0.0.1（和 oams serve 一致）。
#   OAMS_BIND_PORT       可选。监听端口，默认 8000；实际地址看启动前打印的那一行。
#   OAMS_DEV_AUTOLOGIN   可选。默认 1＝本机免登录；=0 恢复登录页。
set -eu

cd "$(dirname "$0")/.."

# 先查连接串：缺了就立刻退出，不要先动 docker 再说缺变量。
if [[ -z "${OAMS_DATABASE_URL:-}" ]]; then
  cat >&2 <<'EOF'
缺少数据库连接串：请先设 OAMS_DATABASE_URL 再跑这个脚本。

  OAMS_DATABASE_URL='postgresql://<账号>:<口令>@127.0.0.1:5432/<库名>' scripts/start-local-trial.sh

脚本只认这一个变量，不会读 .env、也不会猜默认库——本机演示站曾经就因为「没给就用别的库」跑到了错误的库上。
库还不存在时先按 README「一条命令跑起来」建库并跑 oams db init。
EOF
  exit 2
fi
export OAMS_DATABASE_URL

DB_CONTAINER="${OAMS_DB_CONTAINER:-oams-pg}"
if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' | grep -qx "$DB_CONTAINER"; then
    : # 容器已经在运行
  elif docker container inspect "$DB_CONTAINER" >/dev/null 2>&1; then
    echo "数据库容器 $DB_CONTAINER 没在运行，正在启动…"
    docker start "$DB_CONTAINER" >/dev/null
    sleep 3
  else
    echo "没找到名为 $DB_CONTAINER 的 docker 容器，按外部数据库继续（库不是 docker 起的就不影响）。"
  fi
else
  echo "本机没有 docker 命令，跳过容器步骤，按外部数据库继续。"
fi

if [[ ! -x .venv/bin/python ]]; then
  cat >&2 <<'EOF'
没有找到 .venv/bin/python：先装依赖再跑。

  python3 -m venv .venv && . .venv/bin/activate && pip install -e .

（机器上的 python3 版本不够时，README「一条命令跑起来」里有 uv 那条路。）
EOF
  exit 2
fi

BIND_HOST="${OAMS_BIND_HOST:-127.0.0.1}"
BIND_PORT="${OAMS_BIND_PORT:-8000}"
# 监听地址是「所有网卡」时，打印一个能直接点开的地址。
SHOWN_HOST="$BIND_HOST"
[[ "$BIND_HOST" == "0.0.0.0" ]] && SHOWN_HOST="127.0.0.1"

# 设计开发阶段免登录：这个脚本启动的是本机演示站，登录只会挡自己。它只决定「我是谁」，
# 权限、部门范围、审计一条不少，页面右下角会一直显示「开发模式 · 免登录」角标。
# 恢复登录：OAMS_DEV_AUTOLOGIN=0 ./scripts/start-local-trial.sh
export OAMS_DEV_AUTOLOGIN="${OAMS_DEV_AUTOLOGIN:-1}"
echo "网站即将启动： http://${SHOWN_HOST}:${BIND_PORT}"
echo "按 Ctrl+C 停止网站（数据库继续运行）。"
exec .venv/bin/python -m uvicorn oams.api:app --host "$BIND_HOST" --port "$BIND_PORT"
