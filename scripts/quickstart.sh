#!/usr/bin/env bash
# quickstart.sh —— 一条命令从零到首页。
#
# 它替你做完这几步（每一步重复跑都安全）：
#   1. 按机器上的 Python 挑一条路：有 3.11+ 用 venv + pip；只有老 Python（macOS 自带 3.9）就用 uv 管一个 3.12
#   2. 装依赖（pip install -e . / uv pip install -e .）
#   3. 建表（oams db init）、建管理员（oams bootstrap-admin）
#   4. 起服务，并打印一个能直接点开的登录地址
#
# 库地址只认两个来源，按优先级：--database-url、环境变量 OAMS_DATABASE_URL。
# 这个脚本**不写 .env**，也不改你已有的 .env：地址只给这一次运行用。
#
# 用法：
#   bash scripts/quickstart.sh --database-url "postgresql://<账号>:<口令>@127.0.0.1:<端口>/<库名>"
#   OAMS_DATABASE_URL=... bash scripts/quickstart.sh
#   bash scripts/quickstart.sh --database-url ... --port 8080 --admin-code ADMIN001
#
# 前置：库里得先有那个库（脚本只建表，不替你建库）；端口默认 8000，可用 --port 或 OAMS_BIND_PORT 改。

set -euo pipefail

DB_URL=""
HOST="127.0.0.1"
PORT="${OAMS_BIND_PORT:-8000}"
ADMIN_NAME="系统管理员"
ADMIN_CODE="ADMIN001"
ORG_CODE="COMPANY"
ORG_NAME="公司"
VENV_DIR="${OAMS_VENV_DIR:-.venv}"

die() {
  printf '错误：%s\n' "$1" >&2
  exit "${2:-2}"
}

note() {
  printf '==> %s\n' "$1"
}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --database-url)
      [ "$#" -ge 2 ] || die "--database-url 后面要跟一个连接串"
      DB_URL="$2"; shift 2 ;;
    --database-url=*)
      DB_URL="${1#*=}"; shift ;;
    --port)
      [ "$#" -ge 2 ] || die "--port 后面要跟端口号"
      PORT="$2"; shift 2 ;;
    --port=*)
      PORT="${1#*=}"; shift ;;
    --host)
      [ "$#" -ge 2 ] || die "--host 后面要跟地址"
      HOST="$2"; shift 2 ;;
    --host=*)
      HOST="${1#*=}"; shift ;;
    --admin-code)
      [ "$#" -ge 2 ] || die "--admin-code 后面要跟员工编号"
      ADMIN_CODE="$2"; shift 2 ;;
    --admin-code=*)
      ADMIN_CODE="${1#*=}"; shift ;;
    --admin-name)
      [ "$#" -ge 2 ] || die "--admin-name 后面要跟姓名"
      ADMIN_NAME="$2"; shift 2 ;;
    --admin-name=*)
      ADMIN_NAME="${1#*=}"; shift ;;
    --org-code)
      [ "$#" -ge 2 ] || die "--org-code 后面要跟组织编码"
      ORG_CODE="$2"; shift 2 ;;
    --org-code=*)
      ORG_CODE="${1#*=}"; shift ;;
    --org-name)
      [ "$#" -ge 2 ] || die "--org-name 后面要跟组织名"
      ORG_NAME="$2"; shift 2 ;;
    --org-name=*)
      ORG_NAME="${1#*=}"; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      die "不认识的参数：$1（--help 看用法）" ;;
  esac
done

# 1. 库地址：缺了就点名要哪个变量，别让人猜。
if [ -z "${DB_URL// /}" ]; then
  DB_URL="${OAMS_DATABASE_URL:-}"
fi
if [ -z "${DB_URL// /}" ]; then
  cat >&2 <<'EOF'
错误：没有数据库地址。

用 --database-url 传，或者先 export OAMS_DATABASE_URL：

  bash scripts/quickstart.sh --database-url "postgresql://<账号>:<口令>@127.0.0.1:<端口>/<库名>"
  export OAMS_DATABASE_URL="postgresql://<账号>:<口令>@127.0.0.1:<端口>/<库名>"

库里要先有那个库：这个脚本只建表（oams db init），不替你建库。
EOF
  exit 2
fi
export OAMS_DATABASE_URL="$DB_URL"
export OAMS_BIND_HOST="$HOST"
export OAMS_BIND_PORT="$PORT"

# 2. 挑解释器：3.11+ 走 pip；否则用 uv 管一个 3.12（macOS 自带的 python3 是 3.9）。
python_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' >/dev/null 2>&1
}

INSTALL_MODE=""
if command -v python3 >/dev/null 2>&1 && python_ok python3; then
  INSTALL_MODE="pip"
  note "用 python3（$(python3 -V 2>&1)）建虚拟环境：$VENV_DIR"
elif command -v uv >/dev/null 2>&1; then
  INSTALL_MODE="uv"
  note "机器上的 python3 不到 3.11，改用 uv 管一个 Python 3.12 到 ${VENV_DIR}（首次会下载，约十几秒）"
else
  cat >&2 <<'EOF'
错误：这台机器上既没有 3.11+ 的 python3，也没有 uv。

二选一：
  brew install python@3.11
  brew install uv
EOF
  exit 2
fi

# 已有的 .venv 能用就接着用（别每次重建、也别把刚装好的依赖冲掉）。
venv_ready() {
  [ -x "$VENV_DIR/bin/python" ] && python_ok "$VENV_DIR/bin/python"     && [ -f "$VENV_DIR/pyvenv.cfg" ]
}

# 装依赖：有 pip 用 pip，没有就借 uv，连 uv 也没有就用 venv 自带的 ensurepip 补一个。
install_into_venv() {
  if "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
    note "装依赖（pip install -e .）"
    "$VENV_DIR/bin/python" -m pip install -q -e .
  elif command -v uv >/dev/null 2>&1; then
    note "装依赖（uv pip install -e .）"
    VIRTUAL_ENV="$VENV_DIR" uv pip install -q -e .
  else
    note "虚拟环境里没有 pip，先补一个再装依赖"
    "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null
    "$VENV_DIR/bin/python" -m pip install -q -e .
  fi
}

# 不是虚拟环境、或者建出来用不了的残留目录，先清掉（上一步失败留下的常见坑）。
clear_broken_venv() {
  case "$VENV_DIR" in
    ""|"/"|"."|"..")
      die "虚拟环境目录名不对：'$VENV_DIR'" ;;
  esac
  if [ -e "$VENV_DIR" ] && [ ! -f "$VENV_DIR/pyvenv.cfg" ]; then
    note "发现 $VENV_DIR 不是虚拟环境（多半是上一步失败留下的），清掉重建"
    rm -rf -- "$VENV_DIR"
  elif [ -e "$VENV_DIR" ] && ! "$VENV_DIR/bin/python" -c '' >/dev/null 2>&1; then
    note "发现一个用不了的 ${VENV_DIR}，清掉重建"
    rm -rf -- "$VENV_DIR"
  fi
}

if [ "$INSTALL_MODE" = "pip" ]; then
  clear_broken_venv
  if venv_ready; then
    note "复用已有的 ${VENV_DIR}（$("$VENV_DIR/bin/python" -V 2>&1)）"
  else
    if [ -x "$VENV_DIR/bin/python" ]; then
      note "$VENV_DIR 里的 Python 不到 3.11，清掉重建"
      python3 -m venv --clear "$VENV_DIR"
    else
      python3 -m venv "$VENV_DIR"
    fi
  fi
  install_into_venv
else
  clear_broken_venv
  if venv_ready; then
    note "复用已有的 ${VENV_DIR}（$("$VENV_DIR/bin/python" -V 2>&1)）"
  else
    # 正经虚拟环境交给 uv 自己 --clear 覆盖；uv 只肯清虚拟环境，普通目录上面已经清掉了。
    note "用 uv 建虚拟环境：${VENV_DIR}（复用 uv 管好的 Python 3.12）"
    uv venv --clear --python 3.12 "$VENV_DIR" >/dev/null
  fi
  install_into_venv
fi

PY="$VENV_DIR/bin/python"
OAMS="$VENV_DIR/bin/oams"
[ -x "$PY" ] || die "虚拟环境没建起来：$VENV_DIR/bin/python 不存在"
[ -x "$OAMS" ] || die "命令行没装上：$OAMS 不存在（依赖安装失败，看上面的输出）"

# 3. 建表 + 建管理员（重复跑都安全）。
note "建表（oams db init）"
"$OAMS" db init

note "建管理员 ${ADMIN_CODE}（已经有了就跳过）"
if ! BOOTSTRAP_OUT="$("$OAMS" bootstrap-admin --name "$ADMIN_NAME" --employee-code "$ADMIN_CODE" --org-code "$ORG_CODE" --org-name "$ORG_NAME" 2>&1)"; then
  case "$BOOTSTRAP_OUT" in
    *"already exists"*|*"refusing"*|*已经存在*)
      note "库里已经有管理员了，跳过建管理员"
      ;;
    *)
      printf '%s\n' "$BOOTSTRAP_OUT" >&2
      die "建管理员失败（上面是它的输出）"
      ;;
  esac
else
  printf '%s\n' "$BOOTSTRAP_OUT"
fi

# 4. 起服务前先签一张令牌，打印能直接点开的地址。
BASE_URL="http://$HOST:$PORT"
LOGIN_URL="$("$OAMS" issue-token --employee-code "$ADMIN_CODE" --base-url "$BASE_URL" 2>/dev/null \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("login_url",""))' 2>/dev/null || true)"

printf '\n'
note "服务地址：$BASE_URL"
if [ -n "$LOGIN_URL" ]; then
  note "点这个直接进（这张令牌只给本机试用，别贴到聊天或工单里）："
  printf '    %s\n' "$LOGIN_URL"
else
  note "没能自动签令牌；打开 $BASE_URL 后用 oams issue-token 登录"
fi
note "按 Ctrl+C 停止服务"
printf '\n'

exec "$OAMS" serve
