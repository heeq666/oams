#!/usr/bin/env bash
#
# Run the whole test suite in one command.
#
#   1. the pure-logic suites (descriptor / render / modules / metrics / funnel /
#      mcp) -- they never open a database, so they run anywhere;
#   2. the database-backed suites (@pytest.mark.db), which bring up a throwaway
#      PostgreSQL database and serve the app against it.  When no PostgreSQL is
#      reachable those tests *skip* instead of failing, so this script stays
#      usable on a laptop without Docker.
#
# Exit code is 0 only when both halves are green (skips are not failures).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

export WORKHUB_DATABASE_URL="${WORKHUB_DATABASE_URL:-postgresql://workhub:change-me@127.0.0.1:55433/workhub}"
export PYTHONPATH="$ROOT/app${PYTHONPATH:+:$PYTHONPATH}"

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

unit_status=0
db_status=0

echo "== pure-logic suite (no database) =="
set +e
"$PYTHON" -m pytest -q -m "not db" "$@" 2>&1 | tee "$TMPDIR_RUN/unit.log"
unit_status=${PIPESTATUS[0]}
set -e

echo
echo "== database suite (@pytest.mark.db, skipped when no PostgreSQL) =="
echo "   WORKHUB_DATABASE_URL=$WORKHUB_DATABASE_URL"
set +e
"$PYTHON" -m pytest -q -m "db" "$@" 2>&1 | tee "$TMPDIR_RUN/db.log"
db_status=${PIPESTATUS[0]}
set -e

unit_line="$(grep -E '(passed|failed|error|no tests ran|skipped)' "$TMPDIR_RUN/unit.log" | tail -n 1 || true)"
db_line="$(grep -E '(passed|failed|error|no tests ran|skipped)' "$TMPDIR_RUN/db.log" | tail -n 1 || true)"

overall="OK"
if [[ $unit_status -ne 0 || $db_status -ne 0 ]]; then
  overall="FAILED"
fi

echo
echo "------------------------------------------------------------"
printf 'pure-logic : %s\n' "${unit_line:-no result}"
printf 'database   : %s\n' "${db_line:-no result}"
printf 'summary    : %s (unit exit=%s, db exit=%s)\n' "$overall" "$unit_status" "$db_status"
echo "------------------------------------------------------------"

if [[ $unit_status -ne 0 ]]; then
  exit $unit_status
fi
if [[ $db_status -ne 0 ]]; then
  exit $db_status
fi
exit 0
