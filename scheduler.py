#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻调度守护：每 5 分钟一轮心跳，按 sources 表配置逐源限量下载。

"每小时十册、连续数月跑完一个馆"的落地形态：
  * 每轮心跳对每个 enabled 源调 run_source_heartbeat(quota=hourly_quota)；
  * HourQuota 滑动窗口保证任意 1 小时内启动的册数 ≤ 配额（大册跨心跳续跑由
    页级断点保证安全；重启后配额清零，无碍）；
  * 事件写库（web 任务面板可读）+ 本地日志。

用法:
  nohup python3 scheduler.py >> data/logs/scheduler.log 2>&1 &

Docker 部署后由容器 ENTRYPOINT 直接运行本文件（与 web 同容器）。
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import DB                               # noqa: E402
from core.http import HttpClient                     # noqa: E402
from core.pipeline import run_source_heartbeat       # noqa: E402

DATA_DIR = os.environ.get("EIGHTNATIONS_DATA",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "data"))
DB_PATH = os.path.join(DATA_DIR, "db", "library.db")
HEARTBEAT_SEC = int(os.environ.get("EIGHTNATIONS_HEARTBEAT", "300"))
CATALOG_MAX_AGE_S = 7 * 86400        # 目录每周巡检一次（direct 策略馆）


def _stale(ts: str) -> bool:
    """目录是否需要（重新）收割：从未收割或超过 7 天。"""
    if not ts:
        return True
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() > CATALOG_MAX_AGE_S
    except ValueError:
        return True


def seed_catalog(d: DB, s) -> None:
    """direct 策略馆：站点可直连，由容器自动收割目录（新书发现）。

    snapshot 策略馆（如 loc）在盾后，不自动收割——通过 web 上传快照
    或 tools/loc_snapshot.py 产出后导入。
    """
    from sites import get_adapter
    try:
        adapter = get_adapter(s["id"], HttpClient())
    except KeyError:
        return
    harvest = getattr(adapter, "harvest_fonds", None)
    if not harvest:
        return
    print(f"[scheduler] {s['id']}: 目录自动收割 {s['catalog_url']}", flush=True)
    try:
        metas = harvest(s["catalog_url"], max_pages=50)
    except Exception as e:
        d.log(f"目录收割失败: {e}", level="warn", source=s["id"])
        print(f"[scheduler] {s['id']}: 收割失败 {e}", flush=True)
        return
    new = sum(1 for m in metas if d.upsert_book(s["id"], m.__dict__))
    if metas:
        d.set_catalog_time(s["id"])     # 空结果(如站点限流)不标记，下个心跳重试
    d.log(f"目录自动收割: {len(metas)} 条（新书 {new}），待人工审核",
          source=s["id"])
    print(f"[scheduler] {s['id']}: 收割 {len(metas)} 条（新书 {new}）", flush=True)


def main() -> None:
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    d = DB(DB_PATH)
    d.init()
    print(f"[scheduler] 启动: 心跳 {HEARTBEAT_SEC}s, db={DB_PATH}", flush=True)
    while True:
        try:
            with d.connect() as conn:
                sources = conn.execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM books b "
                    " WHERE b.source_id=s.id AND b.status='queued') AS pending,"
                    " (SELECT COUNT(*) FROM books b WHERE b.source_id=s.id)"
                    " AS total FROM sources s WHERE s.enabled=1").fetchall()
            for s in sources:
                # 1) 目录发现（direct 策略：空库首次收割 / 每周巡检）
                if s["meta_strategy"] == "direct" and s["catalog_url"] \
                        and _stale(s["last_catalog_at"]):
                    seed_catalog(d, s)
                # 2) 下载心跳
                if not s["pending"]:
                    continue
                tried, ok, hit = run_source_heartbeat(
                    d, s["id"], s["hourly_quota"], s["quality"])
                print(f"[scheduler] {s['id']}: 尝试 {tried} 成功 {ok}"
                      f"{'(配额尽)' if hit else ''}，剩 {s['pending'] - tried} 在队列",
                      flush=True)
        except KeyboardInterrupt:
            print("[scheduler] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[scheduler] 心跳异常(继续): {e}", flush=True)
            d.log(f"调度心跳异常: {e}", level="warn", source="scheduler")
        time.sleep(HEARTBEAT_SEC)


if __name__ == "__main__":
    main()
