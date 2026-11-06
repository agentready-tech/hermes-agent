"""Linux memory of authoritative gateway PIDs and descendants, excluding the controller."""
from pathlib import Path
import statistics
import threading


def descendants(roots):
    parents = {}
    for file in Path('/proc').glob('[0-9]*/stat'):
        try:
            rest = file.read_text().rsplit(')', 1)[1].split()
            parents[int(file.parent.name)] = int(rest[1])
        except (FileNotFoundError, ProcessLookupError):
            continue
    selected = set(roots)
    while more := {pid for pid, ppid in parents.items() if ppid in selected} - selected:
        selected.update(more)
    return selected


def memory(pid):
    values = {}
    for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == 'kB':
            values[parts[0].rstrip(':')] = int(parts[1]) * 1024
    return {k: values.get(k, 0) for k in ('Rss', 'Pss', 'Pss_Anon', 'Private_Clean', 'Private_Dirty')}


def sample(roots):
    rows = [memory(pid) for pid in descendants(roots)]
    total = {key: sum(row[key] for row in rows) for key in rows[0]}
    cgroup = Path('/sys/fs/cgroup/memory.current')
    total['cgroupCurrentIncludingController'] = int(cgroup.read_text()) if cgroup.exists() else None
    cgroup_stat = Path('/sys/fs/cgroup/memory.stat')
    total['cgroupAnonIncludingController'] = (int(dict(line.split() for line in cgroup_stat.read_text().splitlines())['anon'])
                                            if cgroup_stat.exists() else None)
    total['runtimeProcessCount'] = len(rows)
    return total


def measure(roots, count=7):
    samples = []
    for _ in range(count):
        samples.append(sample(roots))
        threading.Event().wait(.2)
    medians = {k: statistics.median(row[k] for row in samples)
               for k in samples[0] if samples[0][k] is not None}
    return {'samples': samples, 'medianBytes': medians, 'authoritativeGatewayPids': list(roots)}
