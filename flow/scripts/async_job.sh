#!/usr/bin/env bash
# Default launcher of one async report job (ORFS_ASYNC_REPORTS=1, scripts/async_reports.tcl).
# usage: async_job.sh <job dir> <name>
# Runs scripts/async_report.tcl in a separate OpenROAD process with the step's environment (OPENROAD_EXE,
# OPENROAD_ARGS, SCRIPTS_DIR, RESULTS_DIR, ...) and parameters <job dir>/<name>.params.tcl. Writes
# <name>.start, <name>.log, <name>.json (metrics), <name>.time (with GNU time), <name>.end and, last,
# <name>.exit with the OpenROAD exit status; the end-of-flow join (async_reports_merge.py --wait) waits for
# <name>.exit. Set ORFS_ASYNC_JOB_SH to use another launcher with the same contract.
D=$1
N=$2
date +%s.%N > "$D/$N.start"
export ORFS_ASYNC_REPORTS=0 ORFS_ASYNC_PARAMS="$D/$N.params.tcl" ORFS_STEP="$N"
TIME_CMD=()
if [ -x /usr/bin/time ]; then
  TIME_CMD=(/usr/bin/time -f 'TIME_V maxrss_kib=%M user_s=%U sys_s=%S wall_s=%e' -o "$D/$N.time")
fi
# OPENROAD_ARGS is a list of options, split on purpose
# shellcheck disable=SC2086
"${TIME_CMD[@]}" "$OPENROAD_EXE" -exit $OPENROAD_ARGS -no_splash -metrics "$D/$N.json" \
  "$SCRIPTS_DIR/async_report.tcl" > "$D/$N.log" 2>&1
rc=$?
date +%s.%N > "$D/$N.end"
echo $rc > "$D/$N.exit.tmp"
mv "$D/$N.exit.tmp" "$D/$N.exit"
