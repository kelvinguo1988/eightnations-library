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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import DB                               # noqa: E402
from core.pipeline import run_source_heartbeat       # noqa: E402

DATA_DIR = os.environ.get("EIGHTNATIONS_DATA",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "data"))
DB_PATH = os.path.join(DATA_DIR, "db", "library.db")
HEARTBEAT_SEC = int(os.environ.get("EIGHTNATIONS_HEARTBEAT", "300"))


def main() -> None:
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    d = DB(DB_PATH)
    d.init()
    print(f"[scheduler] 启动: 心跳 {HEARTBEAT_SEC}s, db={DB_PATH}", flush=True)
    while True:
        try:
            with d.connect() as conn:
                sources = conn.execute(
                    "SELECT s.id, s.hourly_quota, s.quality, "
                    "(SELECT COUNT(*) FROM books b WHERE b.source_id=s.id "
                    " AND b.status='queued') AS pending "
                    "FROM sources s WHERE s.enabled=1").fetchall()
            for s in sources:
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
