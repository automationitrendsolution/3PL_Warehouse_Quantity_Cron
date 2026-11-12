#!/usr/bin/env bash
# Cron wrapper for the weekly 3PL stock sync. What cron lacks, set here: the job's own
# directory, a UTF-8 locale, the .venv interpreter, an overlap lock and a dated log.
#   ./run_3pl_warehouse.sh [--dry-run] [--only tactical,gti,ideal,fbaprep] [--headed]
set -Eeuo pipefail
JOB=3pl_warehouse
JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$JOB_DIR"

export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" PYTHONIOENCODING=utf-8
# Cron's HOME is set, so Playwright finds its browsers in ~/.cache/ms-playwright as usual.
PYTHON_BIN="${PYTHON_BIN:-$JOB_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"

mkdir -p logs
# Keep 12 weeks of run logs.
find logs -maxdepth 1 -name "${JOB}_*.log" -mtime +84 -delete 2>/dev/null || true

if command -v flock >/dev/null 2>&1; then
  exec 9>"$JOB_DIR/.$JOB.lock"
  if ! flock -n 9; then
    echo "$(date -Is) another run is still in progress; skipping" | tee -a logs/cron.log >&2
    exit 0
  fi
fi

status=0
if [[ -t 1 ]]; then
  # By hand: the live Rich dashboard on screen. Python writes the dated log itself, so no tee
  # (piping would hide the terminal and switch the dashboard off).
  "$PYTHON_BIN" update_3pl_warehouse.py "$@" || status=$?
else
  # Cron: the dated log (logs/3pl_warehouse_YYYYMMDD.log) is written by Python. Only a crash
  # before logging starts (bad interpreter, import error) lands in logs/cron.log.
  "$PYTHON_BIN" update_3pl_warehouse.py "$@" >/dev/null 2>>logs/cron.log || status=$?
fi
exit "$status"
