"""Local synthetic-data benchmark: python -m tests.benchmark_database.

Use --baseline path/to/groups_before.py to compare a saved GroupManager.
Only creates temporary databases under .tools; never calls Hook or a model.
"""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from statistics import median
import tempfile
import time

from wxhook_api.services.groups import GroupManager


async def measure(manager, groups, rosters):
    changes = []
    original = manager._connect

    @contextmanager
    def observe():
        with original() as db:
            yield db
            changes.append(db.total_changes)

    manager._connect = observe
    await manager.radar(groups[0]["id"])
    radar_times, sync_times = [], []
    for _ in range(5):
        start = time.perf_counter()
        result = await manager.radar(groups[0]["id"])
        radar_times.append(time.perf_counter() - start)
    for _ in range(5):
        start = time.perf_counter()
        await manager.sync_contacts(groups, rosters)
        sync_times.append(time.perf_counter() - start)
    return dict(radar_median_seconds=round(median(radar_times), 4),
                roster_median_seconds=round(median(sync_times), 4),
                roster_sqlite_row_changes=changes[-1]), result


async def main(baseline_path):
    root = Path(__file__).resolve().parents[1] / ".tools"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="database-benchmark-", dir=root) as folder:
        path = Path(folder) / "benchmark.sqlite3"
        manager = GroupManager(path)
        await manager.start([])
        groups = [{"id": f"g{i}@chatroom", "name": f"group {i}"} for i in range(50)]
        rosters = {group["id"]: [{"id": f"m{i}", "name": f"member {i}"} for i in range(400)] for group in groups}
        await manager.sync_contacts(groups, rosters)
        china = timezone(timedelta(hours=8))
        start = int((datetime.now(china).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=13)).timestamp())
        with manager._connect() as db:
            db.executemany("""INSERT INTO chat_messages
                (group_id,msg_key,msgid,sender,type,timestamp,content,received_at,source,raw_json)
                VALUES (?,?,?,?,?,?,?,'','history','{}')""",
                ((groups[0]["id"], str(i), str(i), f"m{i % 400}", 3 if i % 5 == 0 else 1,
                  start + (i * 11) % (14 * 86400), "苹果 香蕉 模型 测试") for i in range(100_000)))
        report = {"scenario": "100000 radar messages; 50 unchanged rosters of 400 members; median of 5 runs"}
        before_result = None
        if baseline_path:
            spec = importlib.util.spec_from_file_location("benchmark_baseline_groups", baseline_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            before = module.GroupManager(path)
            report["before"], before_result = await measure(before, groups, rosters)
        report["current"], current_result = await measure(manager, groups, rosters)
        if before_result is not None:
            assert before_result == current_result, "Radar output differs from baseline"
            report["radar_outputs_identical"] = True
            report["radar_speedup"] = round(report["before"]["radar_median_seconds"] / report["current"]["radar_median_seconds"], 2)
            report["roster_speedup"] = round(report["before"]["roster_median_seconds"] / report["current"]["roster_median_seconds"], 2)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    asyncio.run(main(parser.parse_args().baseline))
