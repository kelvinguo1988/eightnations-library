#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻调度守护：默认每 15 分钟一轮心跳，按 sources 表配置逐源限量下载。

"每小时十册、连续数月跑完一个馆"的落地形态：
  * 每轮心跳：① direct 策略馆的目录增量收割（预算制，防封禁）
              ② 从 queued 取书下载（每源每小时 ≤ hourly_quota 册，滑动窗口）；
  * HourQuota 常驻实例保证滑动窗口跨心跳生效（每小时配额是真实的小时窗）；
  * 心跳间隔用环境变量 EIGHTNATIONS_HEARTBEAT（秒）调整，默认 900；
  * 大册跨心跳续跑由页级断点保证安全；重启后进程内配额清零，无碍；
  * 事件写库（web 任务面板可读）+ 本地日志。

用法:
  nohup python3 scheduler.py >> data/logs/scheduler.log 2>&1 &

Docker 部署后由容器 ENTRYPOINT 直接运行本文件（与 web 同容器）。
"""
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import DB                               # noqa: E402
from core.http import HttpClient                     # noqa: E402
from core.pipeline import run_source_heartbeat       # noqa: E402

DATA_DIR = os.environ.get("EIGHTNATIONS_DATA",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "data"))
DB_PATH = os.path.join(DATA_DIR, "db", "library.db")
HEARTBEAT_SEC = int(os.environ.get("EIGHTNATIONS_HEARTBEAT", "900"))
CATALOG_MAX_AGE_S = 7 * 86400        # 目录每周巡检一次（direct 策略馆）
CATALOG_BUDGET = int(os.environ.get("EIGHTNATIONS_CATALOG_BUDGET", "40"))
BLOCK_COOLDOWN_S = 30 * 60           # 被站点限流后的冷却期


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


def seed_catalog(d: DB, s) -> bool:
    """direct 策略馆目录收割（增量、预算制，防封禁）。

    * 单次最多 CATALOG_BUDGET 个条目（远低于日本站 ~58 次连续请求的限流阈值），
      未完成部分下个心跳自动续传（逐条即时入库，中断不丢进度）；
    * 已入库条目跳过 → 每周巡检几乎零请求；
    * 遇限流返回 True，调用方安排冷却。

    snapshot 策略馆（如 loc）在盾后，不自动收割——通过 web 上传快照导入。
    """
    from sites import get_adapter
    try:
        adapter = get_adapter(s["id"], HttpClient())
    except KeyError:
        return False
    harvest = getattr(adapter, "harvest_step", None)
    if not harvest:
        return False
    with d.connect() as conn:
        known = {r["source_uid"] for r in conn.execute(
            "SELECT source_uid FROM books WHERE source_id=?", (s["id"],))}
    new = 0

    def on_meta(meta):
        nonlocal new
        if d.upsert_book(s["id"], meta.__dict__):
            new += 1
            row = d.find_book(s["id"], meta.source_uid)
            d.log(f"新书发现: {meta.alt_title or meta.title}",
                  source=s["id"], book_id=row["id"] if row else None)

    try:
        stats = harvest(s["catalog_url"], known_uids=known,
                        budget=CATALOG_BUDGET, on_meta=on_meta)
    except Exception as e:
        d.log(f"目录收割失败: {e}", level="warn", source=s["id"])
        print(f"[scheduler] {s['id']}: 收割失败 {e}", flush=True)
        return False
    if stats.get("blocked"):
        d.log(f"目录收割遇站点限流（已抓 {stats['fetched']} 条），"
              f"{BLOCK_COOLDOWN_S // 60} 分钟后自动续传", level="warn",
              source=s["id"])
        print(f"[scheduler] {s['id']}: ⚠️ 站点限流，冷却后自动续传", flush=True)
        return True
    if stats.get("exhausted"):
        d.set_catalog_time(s["id"])   # 目录全部处理完才标记；否则下个心跳续传
    d.log(f"目录收割: 总 {stats['ids']} 条，新抓 {stats['fetched']}，"
          f"跳过已存在 {stats['skipped']}（新书 {new}）", source=s["id"])
    print(f"[scheduler] {s['id']}: 目录收割 总{stats['ids']} 新抓{stats['fetched']} "
          f"跳过{stats['skipped']} 新书{new}", flush=True)
    return False


def main() -> None:
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    d = DB(DB_PATH)
    d.init()
    blocked_until: Dict[str, float] = {}
    quotas: Dict[str, "object"] = {}     # 常驻实例 → "每小时 N 册"滑动窗口跨心跳生效
    print(f"[scheduler] 启动: 心跳 {HEARTBEAT_SEC}s, 目录预算/心跳 {CATALOG_BUDGET} "
          f"条, db={DB_PATH}", flush=True)
    while True:
        try:
            with d.connect() as conn:
                sources = conn.execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM books b "
                    " WHERE b.source_id=s.id AND b.status='queued') AS pending,"
                    " (SELECT COUNT(*) FROM books b WHERE b.source_id=s.id)"
                    " AS total FROM sources s WHERE s.enabled=1").fetchall()
            for s in sources:
                # 1) 目录发现（direct 策略：空库首次收割 / 未完成续传 / 每周巡检）
                if s["meta_strategy"] == "direct" and s["catalog_url"] \
                        and _stale(s["last_catalog_at"]):
                    if time.time() < blocked_until.get(s["id"], 0):
                        pass        # 限流冷却中，本轮跳过
                    elif seed_catalog(d, s):
                        blocked_until[s["id"]] = time.time() + BLOCK_COOLDOWN_S
                # 2) 下载心跳（常驻 HourQuota：每小时 ≤ hourly_quota 册）
                if not s["pending"]:
                    continue
                quota = quotas.get(s["id"])
                if quota is None or getattr(quota, "default_quota", None) \
                        != s["hourly_quota"]:
                    from core.limiter import HourQuota
                    quota = HourQuota(default_quota=s["hourly_quota"])
                    quotas[s["id"]] = quota
                tried, ok, hit = run_source_heartbeat(
                    d, s["id"], s["hourly_quota"], s["quality"], quota=quota)
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
