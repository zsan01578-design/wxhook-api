"""Synthetic management reads: python -m tests.benchmark_management_reads.

Pass --baseline path/to/groups_before.py to compare a saved implementation.
Uses isolated copies of the same data so index benefits are measured separately.
"""

import argparse
import asyncio
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
from statistics import median
import tempfile
import time

from wxhook_api.core.douyin_config import GroupSettings
from wxhook_api.services.groups import GroupManager


async def measure(manager):
    async def policies():
        await asyncio.to_thread(manager._reload)
        return manager._policies, manager._blocked_members

    operations = {
        "group_list": lambda: manager.groups(GroupSettings()),
        "blocked_members": lambda: manager.members(blocked_only=True),
        "policy_reload": policies,
    }
    timings, results = {}, {}
    for name, operation in operations.items():
        await operation()
        samples = []
        for _ in range(7):
            start = time.perf_counter()
            results[name] = await operation()
            samples.append(time.perf_counter() - start)
        timings[name + "_seconds"] = round(median(samples), 5)
    return timings, results


async def main(baseline_path):
    baseline_type = GroupManager
    if baseline_path:
        spec = importlib.util.spec_from_file_location("management_baseline_groups", baseline_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        baseline_type = module.GroupManager
    root = Path(__file__).resolve().parents[1] / ".tools"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="management-read-benchmark-", dir=root) as folder:
        assert Path(folder).resolve().is_relative_to(root.resolve())
        before_path, current_path = Path(folder) / "before.sqlite3", Path(folder) / "current.sqlite3"
        before = baseline_type(before_path)
        await before.start([])
        with before._connect() as db:
            db.executemany("INSERT INTO groups(id,name,created_at,pinned) VALUES (?,?,'2024-01-01',?)",
                           ((f"g{g}@chatroom", f"group {g}", int(g % 11 == 0)) for g in range(200)))
            db.executemany("""INSERT INTO members(group_id,id,wechat_name,blocked,membership)
                VALUES (?,?,?,?,?)""",
                ((f"g{g}@chatroom", f"m{i}", f"member {i}", int(i % 113 == 0),
                  "current" if i % 3 else "left") for g in range(200) for i in range(500)))
        with before._connect() as source, closing(sqlite3.connect(current_path)) as target:
            source.backup(target)
        current = GroupManager(current_path)
        await current.start([])
        report = {"scenario": "200 groups, 100000 members, 1000 blocked; median of 7 runs"}
        before_times, before_results = await measure(before)
        report["current"], current_results = await measure(current)
        if baseline_path:
            assert before_results == current_results, "Management data or policy output differs"
            report["before"] = before_times
            report["all_outputs_identical"] = True
            report["speedups"] = {name.removesuffix("_seconds"): round(before_times[name] / report["current"][name], 2)
                                  for name in before_times}
            report["additional_index_bytes"] = current_path.stat().st_size - before_path.stat().st_size
        with current._connect() as db:
            report["indexed_plans"] = {
                name: [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql)]
                for name, sql in {
                    "blocked": "SELECT group_id,id FROM members WHERE blocked=1",
                    "current": "SELECT count(*) FROM members WHERE group_id='g0@chatroom' AND membership='current'",
                }.items()
            }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    asyncio.run(main(parser.parse_args().baseline))
