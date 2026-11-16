#!/usr/bin/env python3
"""Merge the metrics of async report_metrics jobs into the step metrics (kit patch v2).

usage: async_reports_merge.py <RESULTS_DIR>/async_reports <LOG_DIR>

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
A summary line per job goes to stdout; the exit status is 1 when a job failed, a placeholder has no job metrics,
a stage does not match, or a warning count came out negative.
"""
import os
import re
import sys

ENTRY = re.compile(r'^\t"(.*)": (.*)$')
SLOT = 'orfs_async__slot:'
WARN_LINE = re.compile(r'^\[WARNING ([A-Z0-9]+-\d{4})\]')
BOOKKEEPING = re.compile(r'flow__(warnings|errors)__')
PER_ID = re.compile(r'flow__warnings__count:([A-Z0-9]+-\d{4})')


def read_entries(path):
    """Logger metrics JSON -> list of [key, raw value text] in file order (keys may repeat)."""
    text = open(path, encoding='utf-8').read()
    lines = text.split('\n')
    if not lines or lines[0].strip() != '{' or lines[-1].strip() != '}':
        raise ValueError(f'{path}: not in the Logger metrics format')
    out = []
    for line in lines[1:-1]:
        if line.endswith(','):
            line = line[:-1]
        m = ENTRY.match(line)
        if not m:
            raise ValueError(f'{path}: unexpected line {line[:80]!r}')
        out.append([m.group(1), m.group(2)])
    return out


def write_entries(path, entries):
    body = ','.join(f'\n\t"{k}": {v}' for k, v in entries)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        f.write('{' + body + '\n}')
    os.replace(path + '.tmp', path)


def section_warnings(log_path):
    """Warning ids printed before and inside the report section of a job log."""
    pre, inside, after = {}, {}, {}
    where = pre
    marks = set()
    for line in open(log_path, encoding='utf-8', errors='replace'):
        if line.startswith('ORFS_ASYNC_JOB section_begin'):
            where = inside
            marks.add('begin')
            continue
        if line.startswith('ORFS_ASYNC_JOB section_end'):
            where = after
            marks.add('end')
            continue
        m = WARN_LINE.match(line)
        if m:
            where[m.group(1)] = where.get(m.group(1), 0) + 1
    return pre, inside, after, marks


def id_order(wid):
    tool, num = wid.rsplit('-', 1)
    return (tool, int(num))


def merge_step(json_path, ad, problems):
    entries = read_entries(json_path)
    if not any(SLOT in k for k, _ in entries):
        return []
    step = os.path.basename(json_path)[:-len('.json')]
    keep = os.path.join(ad, f'{step}.main.json')
    if not os.path.exists(keep):
        with open(keep, 'w', encoding='utf-8') as f:
            f.write(open(json_path, encoding='utf-8').read())
    merged = []
    deltas = {}
    lines = []
    for k, v in entries:
        if SLOT not in k:
            merged.append([k, v])
            continue
        prefix, rest = k.split(SLOT, 1)
        name, sep, tail = rest.partition('__')
        suffix = sep + tail
        jp, lp, ep = (os.path.join(ad, f'{name}.{x}') for x in ('json', 'log', 'exit'))
        status = []
        ex = open(ep).read().strip() if os.path.exists(ep) else 'missing'
        if ex != '0':
            status.append(f'exit={ex}')
        if not os.path.exists(jp) or not os.path.exists(lp):
            status.append('no json/log')
        if status:
            problems.append(f'{step} {name}: ' + ' '.join(status))
            merged.append([k, v])
            lines.append(f'MERGE step={step} job={name} status=FAILED {" ".join(status)}')
            continue
        job = read_entries(jp)
        pre, inside, after, marks = section_warnings(lp)
        if marks != {'begin', 'end'}:
            problems.append(f'{step} {name}: section markers {sorted(marks)}')
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
                problems.append(f'{step} {name}: key {jk} outside stage {prefix}{{}}{suffix}')
            merged.append([jk, jv])
            n += 1
        sec = {}
        for wid in set(job_counts) | set(pre):
            d = job_counts.get(wid, 0) - pre.get(wid, 0)
            if d < 0:
                problems.append(f'{step} {name}: warning {wid} count {job_counts.get(wid, 0)} < printed before the section {pre.get(wid, 0)}')
            if d > 0:
                sec[wid] = d
                deltas[wid] = deltas.get(wid, 0) + d
        if after:
            problems.append(f'{step} {name}: warnings after the section {after}')
        printed = sum(inside.values())
        lines.append(f'MERGE step={step} job={name} status=ok metrics={n} section_warnings='
                     + (','.join(f'{w}:{c}' for w, c in sorted(sec.items())) or '-')
                     + f' printed_in_log={printed} pre_section=' + (','.join(f'{w}:{c}' for w, c in sorted(pre.items())) or '-'))
    if deltas:
        merged = add_warnings(merged, deltas, step, problems)
    write_entries(json_path, merged)
    lines.append(f'MERGE step={step} added_warnings=' + (','.join(f'{w}:{c}' for w, c in sorted(deltas.items())) or '-'))
    return lines


def add_warnings(entries, deltas, step, problems):
    total_i = [i for i, (k, _) in enumerate(entries) if k.endswith('flow__warnings__count')]
    type_i = [i for i, (k, _) in enumerate(entries) if k.endswith('flow__warnings__type_count')]
    if len(total_i) != 1 or len(type_i) != 1:
        problems.append(f'{step}: bookkeeping keys not found once (total {len(total_i)}, type {len(type_i)})')
        return entries
    prefix = entries[total_i[0]][0][:-len('flow__warnings__count')]
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
    block = [[f'{prefix}flow__warnings__count:{w}', str(counts[w])] for w in sorted(counts, key=id_order) if counts[w] > 0]
    ti = next(i for i, (k, _) in enumerate(rest) if k.endswith('flow__warnings__type_count'))
    at = first if first is not None else ti
    rest = rest[:at] + block + rest[at:]
    ti = next(i for i, (k, _) in enumerate(rest) if k.endswith('flow__warnings__type_count'))
    rest[ti][1] = str(len(block))
    return rest


def main():
    ad, logs = sys.argv[1], sys.argv[2]
    problems = []
    for f in sorted(os.listdir(logs)):
        if f.endswith('.json'):
            for line in merge_step(os.path.join(logs, f), ad, problems):
                print(line)
    for p in problems:
        print('MERGE_PROBLEM ' + p)
    print(f'MERGE_DONE problems={len(problems)}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
