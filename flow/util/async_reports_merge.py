#!/usr/bin/env python3
"""Join and merge the async report_metrics jobs of ORFS_ASYNC_REPORTS=1 into the step metrics.

usage: async_reports_merge.py [--wait] <RESULTS_DIR>/async_reports <LOG_DIR>

--wait (the Makefile target async_reports_join, run at the end of "make finish"): first wait until every job
<job>.params.tcl in the directory has its <job>.exit, written last by the launcher (scripts/async_job.sh). A job
whose launcher process (<job>.pid) is gone without writing <job>.exit counts as failed. Prints
"ASYNC_JOIN wait_s=... end_epoch=... jobs=... failed=..." and one ASYNC_JOB_FAILED line per failed job.

The main flow (scripts/async_reports.tcl) leaves a placeholder metric "<stage>orfs_async__slot:<job><suffix>" at
the position an inline report_metrics / analyze_power_grid call would have written its metrics. For every step
metrics file <LOG_DIR>/<step>.json with placeholders this script
  - replaces each placeholder with the metrics of job <job> (async_reports/<job>.json) in their order, leaving out
    the job's own bookkeeping (flow__warnings__* / flow__errors__*); every job key must carry the placeholder's
    stage prefix and suffix;
  - adds the warnings of the job's report section to the step's warning counts: per warning id, the job's count
    (its JSON) minus the warnings its log printed before "ORFS_ASYNC_JOB section_begin" (load, parasitics,
    prewarm); flow__warnings__count, the per-id counts and flow__warnings__type_count are updated;
  - keeps the original file as async_reports/<step>.main.json and writes the merged file in the Logger format.
Step files without placeholders (already merged) are left as they are, so the script can run again.
A summary line per job goes to stdout and to async_reports/merge.log; the exit status is 1 when a job failed, a
placeholder has no job metrics, a stage does not match, or a warning count came out negative.
"""

import os
import re
import sys
import time

ENTRY = re.compile(r'^\t"(.*)": (.*)$')
SLOT = "orfs_async__slot:"
WARN_LINE = re.compile(r"^\[WARNING ([A-Z0-9]+-\d{4})\]")
BOOKKEEPING = re.compile(r"flow__(warnings|errors)__")
PER_ID = re.compile(r"flow__warnings__count:([A-Z0-9]+-\d{4})")


def read_entries(path):
    """Logger metrics JSON -> list of [key, raw value text] in file order (keys may repeat)."""
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    if not lines or lines[0].strip() != "{" or lines[-1].strip() != "}":
        raise ValueError(f"{path}: not in the Logger metrics format")
    out = []
    for line in lines[1:-1]:
        if line.endswith(","):
            line = line[:-1]
        m = ENTRY.match(line)
        if not m:
            raise ValueError(f"{path}: unexpected line {line[:80]!r}")
        out.append([m.group(1), m.group(2)])
    return out


def write_entries(path, entries):
    body = ",".join(f'\n\t"{k}": {v}' for k, v in entries)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("{" + body + "\n}")
    os.replace(path + ".tmp", path)


def section_warnings(log_path):
    """Warning ids printed before and inside the report section of a job log."""
    pre, inside, after = {}, {}, {}
    where = pre
    marks = set()
    for line in open(log_path, encoding="utf-8", errors="replace"):
        if line.startswith("ORFS_ASYNC_JOB section_begin"):
            where = inside
            marks.add("begin")
            continue
        if line.startswith("ORFS_ASYNC_JOB section_end"):
            where = after
            marks.add("end")
            continue
        m = WARN_LINE.match(line)
        if m:
            where[m.group(1)] = where.get(m.group(1), 0) + 1
    return pre, inside, after, marks


def id_order(wid):
    tool, num = wid.rsplit("-", 1)
    return (tool, int(num))


def merge_step(json_path, ad, problems):
    entries = read_entries(json_path)
    if not any(SLOT in k for k, _ in entries):
        return []
    step = os.path.basename(json_path)[: -len(".json")]
    keep = os.path.join(ad, f"{step}.main.json")
    # a file with placeholders is the step's own output (a rerun of the step writes it anew)
    with open(keep, "w", encoding="utf-8") as f:
        f.write(open(json_path, encoding="utf-8").read())
    merged = []
    deltas = {}
    lines = []
    for k, v in entries:
        if SLOT not in k:
            merged.append([k, v])
            continue
        prefix, rest = k.split(SLOT, 1)
        name, sep, tail = rest.partition("__")
        suffix = sep + tail
        jp, lp, ep = (os.path.join(ad, f"{name}.{x}") for x in ("json", "log", "exit"))
        status = []
        ex = open(ep).read().strip() if os.path.exists(ep) else "missing"
        if ex != "0":
            status.append(f"exit={ex}")
        if not os.path.exists(jp) or not os.path.exists(lp):
            status.append("no json/log")
        if status:
            problems.append(f"{step} {name}: " + " ".join(status))
            merged.append([k, v])
            lines.append(
                f'MERGE step={step} job={name} status=FAILED {" ".join(status)}'
            )
            continue
        job = read_entries(jp)
        pre, inside, after, marks = section_warnings(lp)
        if marks != {"begin", "end"}:
            problems.append(f"{step} {name}: section markers {sorted(marks)}")
        job_counts = {}
        n = 0
        for jk, jv in job:
            m = PER_ID.search(jk)
            if m:
                job_counts[m.group(1)] = job_counts.get(m.group(1), 0) + int(jv)
                continue
            if BOOKKEEPING.search(jk):
                continue
            if not (jk.startswith(prefix) and jk.endswith(suffix)):
                problems.append(
                    f"{step} {name}: key {jk} outside stage {prefix}{{}}{suffix}"
                )
            merged.append([jk, jv])
            n += 1
        sec = {}
        for wid in set(job_counts) | set(pre):
            d = job_counts.get(wid, 0) - pre.get(wid, 0)
            if d < 0:
                problems.append(
                    f"{step} {name}: warning {wid} count {job_counts.get(wid, 0)} < printed before the section {pre.get(wid, 0)}"
                )
            if d > 0:
                sec[wid] = d
                deltas[wid] = deltas.get(wid, 0) + d
        if after:
            problems.append(f"{step} {name}: warnings after the section {after}")
        printed = sum(inside.values())
        lines.append(
            f"MERGE step={step} job={name} status=ok metrics={n} section_warnings="
            + (",".join(f"{w}:{c}" for w, c in sorted(sec.items())) or "-")
            + f" printed_in_log={printed} pre_section="
            + (",".join(f"{w}:{c}" for w, c in sorted(pre.items())) or "-")
        )
    if deltas:
        merged = add_warnings(merged, deltas, step, problems)
    write_entries(json_path, merged)
    lines.append(
        f"MERGE step={step} added_warnings="
        + (",".join(f"{w}:{c}" for w, c in sorted(deltas.items())) or "-")
    )
    return lines


def add_warnings(entries, deltas, step, problems):
    total_i = [
        i for i, (k, _) in enumerate(entries) if k.endswith("flow__warnings__count")
    ]
    type_i = [
        i
        for i, (k, _) in enumerate(entries)
        if k.endswith("flow__warnings__type_count")
    ]
    if len(total_i) != 1 or len(type_i) != 1:
        problems.append(
            f"{step}: bookkeeping keys not found once (total {len(total_i)}, type {len(type_i)})"
        )
        return entries
    prefix = entries[total_i[0]][0][: -len("flow__warnings__count")]
    entries[total_i[0]][1] = str(int(entries[total_i[0]][1]) + sum(deltas.values()))
    counts = {}
    first = None
    rest = []
    for i, (k, v) in enumerate(entries):
        m = PER_ID.search(k)
        if m and k.startswith(prefix):
            counts[m.group(1)] = int(v)
            if first is None:
                first = len(rest)
            continue
        rest.append([k, v])
    for wid, d in deltas.items():
        counts[wid] = counts.get(wid, 0) + d
    block = [
        [f"{prefix}flow__warnings__count:{w}", str(counts[w])]
        for w in sorted(counts, key=id_order)
        if counts[w] > 0
    ]
    ti = next(
        i for i, (k, _) in enumerate(rest) if k.endswith("flow__warnings__type_count")
    )
    at = first if first is not None else ti
    rest = rest[:at] + block + rest[at:]
    ti = next(
        i for i, (k, _) in enumerate(rest) if k.endswith("flow__warnings__type_count")
    )
    rest[ti][1] = str(len(block))
    return rest


def launcher_alive(pid_path):
    """False when <job>.pid names a process that is gone (or a zombie); True otherwise, also without a pid file."""
    try:
        pid = int(open(pid_path).read().split()[0])
    except (OSError, ValueError, IndexError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = open(f"/proc/{pid}/stat").read()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def join(ad, emit, poll=0.5, report_every=60.0):
    """Wait for every job in ad; return the number of failed jobs."""
    t0 = time.time()
    last_report = t0
    while True:
        names = sorted(
            f[: -len(".params.tcl")]
            for f in os.listdir(ad)
            if f.endswith(".params.tcl")
        )
        # a job whose launcher is gone has either written its .exit just before or failed (checked below)
        waiting = [
            n
            for n in names
            if not os.path.exists(os.path.join(ad, f"{n}.exit"))
            and launcher_alive(os.path.join(ad, f"{n}.pid"))
        ]
        if not waiting:
            break
        now = time.time()
        if now - last_report >= report_every:
            emit(
                f"ASYNC_JOIN waiting_s={now - t0:.0f} jobs={len(waiting)}: "
                + " ".join(waiting)
            )
            last_report = now
        time.sleep(poll)
    t1 = time.time()
    failed = []
    for n in names:
        ep = os.path.join(ad, f"{n}.exit")
        if os.path.exists(ep):
            ex = open(ep).read().strip()
            if ex != "0":
                failed.append((n, f"exit={ex}"))
        else:
            failed.append((n, "launcher gone without an exit status"))
    for n, why in failed:
        emit(f'ASYNC_JOB_FAILED {n} {why} log={os.path.join(ad, n + ".log")}')
    emit(
        f"ASYNC_JOIN wait_s={t1 - t0:.3f} end_epoch={t1:.3f} jobs={len(names)} failed={len(failed)}"
    )
    return len(failed)


def main():
    args = sys.argv[1:]
    wait = bool(args) and args[0] == "--wait"
    if wait:
        args = args[1:]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    ad, logs = args
    out = []

    def emit(line):
        print(line, flush=True)
        out.append(line)

    if not os.path.isdir(ad):
        emit(f"MERGE_DONE problems=0 (no job directory {ad})")
        return 0
    failed = join(ad, emit) if wait else 0
    problems = []
    for f in sorted(os.listdir(logs)):
        if f.endswith(".json"):
            for line in merge_step(os.path.join(logs, f), ad, problems):
                emit(line)
    for p in problems:
        emit("MERGE_PROBLEM " + p)
    emit(f"MERGE_DONE problems={len(problems)}")
    with open(os.path.join(ad, "merge.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return 1 if problems or failed else 0


if __name__ == "__main__":
    sys.exit(main())
