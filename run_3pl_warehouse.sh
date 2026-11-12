#!/usr/bin/env bash
# Cron wrapper for the weekly 3PL stock sync. What cron lacks, set here: the job's own
# directory, a UTF-8 locale, the .venv interpreter, an overlap lock and one log: logs/cron.log.
#   ./run_3pl_warehouse.sh [--dry-run] [--only tactical,gti,ideal,fbaprep] [--headed]
set -Eeuo pipefail
JOB=3pl_warehouse
JOB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$JOB_DIR"

# Everything this job creates (logs, saved sessions, debug pages) is private to its user.
umask 077
if [[ $EUID -eq 0 ]]; then
  echo "refusing to run as root: run as a normal user (Chromium's sandbox needs it)" >&2
  exit 2
fi

export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}" PYTHONIOENCODING=utf-8
# Cron's HOME is set, so Playwright finds its browsers in ~/.cache/ms-playwright as usual.
PYTHON_BIN="${PYTHON_BIN:-$JOB_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN="$(command -v python3)"

mkdir -p logs
chmod 700 logs logs/debug state 2>/dev/null || true
chmod 600 .env logs/cron.log logs/cron.log.1 2>/dev/null || true
# One log file: logs/cron.log. Past 10 MB it is rolled over to logs/cron.log.1 (one generation).
if [[ -f logs/cron.log ]] && (( $(stat -c %s logs/cron.log) > 10485760 )); then
  mv -f logs/cron.log logs/cron.log.1
fi

if command -v flock >/dev/null 2>&1; then
  exec 9>"$JOB_DIR/.$JOB.lock"
  if ! flock -n 9; then
    echo "$(date -Is) another run is still in progress; skipping" | tee -a logs/cron.log >&2
    exit 0
  fi
fi

status=0
if [[ -t 1 ]]; then
  # By hand: the live Rich dashboard on screen. Python writes logs/cron.log itself, so no tee
  # (piping would hide the terminal and switch the dashboard off).
  "$PYTHON_BIN" update_3pl_warehouse.py "$@" || status=$?
else
  # Cron: Python writes the run log to logs/cron.log itself; a crash before logging starts
  # (bad interpreter, import error) lands there too via stderr.
  "$PYTHON_BIN" update_3pl_warehouse.py "$@" >/dev/null 2>>logs/cron.log || status=$?
fi
exit "$status"
