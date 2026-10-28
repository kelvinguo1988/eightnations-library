#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻调度守护：默认每 15 分钟一轮心跳，按 sources 表配置逐源限量下载。

"每小时十册、连续数月跑完一个馆"的落地形态：
  * 每轮心跳：① direct 策略馆的目录增量收割（预算制，防封禁）
              ② 从 queued 取书下载（每源每小时 ≤ hourly_quota 册，滑动窗口）；
  * 每小时配额以 jobs 表为账本（DB 写事务内原子消费），scheduler 与 Web
    「立即下载」共享同一窗口，重启不清零；
  * running 回收采用租约：持有进程退出或久未续约才回收，不误伤他进程下载；
  * 心跳间隔用环境变量 EIGHTNATIONS_HEARTBEAT（秒）调整，默认 900；
  * 大册跨心跳续跑由页级断点保证安全；
  * 逐源 try/except：单馆异常不影响其他馆，也不跳过回收步骤；
  * 事件写库（web 任务面板可读）+ 本地日志。

用法:
  nohup python3 scheduler.py >> data/logs/scheduler.log 2>&1 &

Docker 部署后由容器 ENTRYPOINT 直接运行本文件（与 web 同容器）。
"""
import os
import sqlite3
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


# harvest_step 的返回契约键（审查 C P0-3）：任何适配器只要缺键或给 None，
# 旧代码 `stats['ids']` 就会 KeyError，被外层"本轮异常"吞掉后目录**永远不会**
# 被标记完成 → 每个心跳都重扫同一页。这里统一归一化，并对 None/非 dict 兜底。
_STATS_KEYS = ("ids", "fetched", "skipped", "blocked", "exhausted")


def _norm_stats(stats) -> dict:
    """把 harvest_step 的返回值归一化成契约字典。

    缺键补 0；None 视为"这一项不可信"（计数按 0 处理，但 exhausted 视为
    不确定 → 不封存）。任何情况下都不向上抛 KeyError/TypeError。
    """
    if not isinstance(stats, dict):
        return {"ids": 0, "fetched": 0, "skipped": 0, "blocked": False,
                "exhausted": False, "reached_end": False, "next_page": 1,
                "failed": 0}
    out = {}
    for k in _STATS_KEYS:
        v = stats.get(k)
        if k == "blocked":
            out[k] = bool(v)
        elif k == "exhausted":
            # None 视为"不确定" → 不封存（宁可多扫一次，也不要漏掉整个目录）
            out[k] = v is True
        else:
            out[k] = int(v) if isinstance(v, (int, float)) else 0
    # 向下透传扩展键（reached_end/next_page/failed 等），缺失给安全默认
    out["reached_end"] = stats.get("reached_end") is True
    out["next_page"] = int(stats["next_page"]) if isinstance(
        stats.get("next_page"), (int, float)) and stats["next_page"] >= 1 else 1
    out["failed"] = int(stats["failed"]) if isinstance(
        stats.get("failed"), (int, float)) else 0
    return out


def seed_catalog(d: DB, s) -> bool:
    """direct 策略馆目录收割（增量、预算制，防封禁）。

    * 单次最多 CATALOG_BUDGET 个条目（远低于日本站 ~58 次连续请求的限流阈值），
      未完成部分下个心跳自动续传（逐条即时入库，中断不丢进度）；
    * 已入库条目跳过 → 每周巡检几乎零请求；
    * 遇限流返回 True，调用方安排冷却。

    snapshot 策略馆（如 loc）在盾后，不自动收割——通过 web 上传快照导入。

    **是否封存目录（set_catalog_time）只在 `exhausted and not blocked` 时执行**：
    exhausted 由适配器按"翻到底且无遗漏"严格判定（审查 C P0-4），
    这里再加一道保险，避免适配器语义漂移时把整个目录封存 7 天。
    """
    from sites import get_adapter
    try:
        adapter = get_adapter(s["id"], HttpClient())
    except KeyError:
        d.log(f"站点 {s['id']} 未注册适配器，跳过目录收割", level="warn",
              source=s["id"])
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

    # 翻页游标：na_jp 的 harvest_step 支持 start_page，旧适配器不支持 → 用
    # 参数检测兼容，避免为此改所有适配器签名（审查 C P0-4 配套）。
    cursor = d.get_catalog_cursor(s["id"])
    kwargs = {"known_uids": known, "budget": CATALOG_BUDGET, "on_meta": on_meta}
    try:
        import inspect as _inspect
        if "start_page" in _inspect.signature(harvest).parameters:
            kwargs["start_page"] = cursor
    except (TypeError, ValueError):
        pass
    try:
        stats = _norm_stats(harvest(s["catalog_url"], **kwargs))
    except Exception as e:
        d.log(f"目录收割失败: {e}", level="warn", source=s["id"])
        print(f"[scheduler] {s['id']}: 收割失败 {e}", flush=True)
        return False
    # 先记日志再决定封存：即便后面判断出错，也已经留下可诊断的痕迹
    d.log(f"目录收割: 总 {stats['ids']} 条，新抓 {stats['fetched']}，"
          f"跳过已存在 {stats['skipped']}（新书 {new}）"
          + (f"，详情失败 {stats['failed']}" if stats["failed"] else "")
          + (f"，翻到底={stats['reached_end']}" if stats["reached_end"] else
             f"，下轮从第 {stats['next_page']} 页续"),
          source=s["id"])
    print(f"[scheduler] {s['id']}: 目录收割 总{stats['ids']} 新抓{stats['fetched']} "
          f"跳过{stats['skipped']} 新书{new} "
          f"{'已翻到底' if stats['reached_end'] else '未完(下轮续)'}", flush=True)
    if stats["blocked"]:
        d.log(f"目录收割遇站点限流（已抓 {stats['fetched']} 条），"
              f"{BLOCK_COOLDOWN_S // 60} 分钟后自动续传", level="warn",
              source=s["id"])
        print(f"[scheduler] {s['id']}: ⚠️ 站点限流，冷却后自动续传", flush=True)
        return True
    if stats["exhausted"] and not stats["blocked"]:
        d.set_catalog_time(s["id"])   # 目录真处理完才封存；否则下个心跳续传
    elif not stats["blocked"] and stats["next_page"] > cursor:
        # 没翻到底 → 记住"下轮从哪页继续"，不再每轮都从第 1 页重扫
        d.set_catalog_cursor(s["id"], stats["next_page"])
    return False


def main() -> None:
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    d = DB(DB_PATH)
    d.init()
    # 崩溃恢复走租约：只回收持有进程已退出/久未续约的 running（Web 进程
    # 正在进行的下载 pid 存活且有心跳，绝不再被无条件重置——那会造成
    # 双进程并发写同一 .part）。此后每个心跳重复执行，兼顾运行期崩溃。
    try:
        n = d.reap_stuck()
        if n:
            d.log(f"启动恢复: 回收 {n} 册中断下载 → queued", source="scheduler")
            print(f"[scheduler] 恢复 {n} 册中断任务 → queued", flush=True)
    except sqlite3.Error as e:
        print(f"[scheduler] 启动回收失败(继续): {e}", flush=True)
    blocked_until: Dict[str, float] = {}
    # 冷却态落库（审查 C P1-2）：内存态在容器重启/崩溃后清零，重启会立刻
    # 再去撞正在封禁我们的站点。sources.last_blocked_at 是持久化的冷却到期时刻。
    try:
        with d.connect() as conn:
            for r in conn.execute("SELECT id, last_blocked_at FROM sources "
                                  "WHERE COALESCE(last_blocked_at,'') <> ''"):
                try:
                    t = datetime.strptime(r["last_blocked_at"],
                                          "%Y-%m-%dT%H:%M:%SZ").replace(
                                              tzinfo=timezone.utc).timestamp()
                    if t > time.time():
                        blocked_until[r["id"]] = t
                except ValueError:
                    pass
    except sqlite3.Error as e:
        print(f"[scheduler] 读取冷却态失败(忽略): {e}", flush=True)
    last_prune = 0.0
    print(f"[scheduler] 启动: 心跳 {HEARTBEAT_SEC}s, 目录预算/心跳 {CATALOG_BUDGET} "
          f"条, db={DB_PATH}", flush=True)
    while True:
        # 固定节拍（审查 C P1-1）：按"本轮开始时刻"算下一次，而不是"本轮结束后再睡"。
        # 旧写法 sleep(HEARTBEAT_SEC) 在轮次耗时长时把周期变成 HEARTBEAT_SEC+耗时，
        # 文档里的"每 15 分钟一轮"名存实亡。
        round_start = time.monotonic()
        try:
            with d.connect() as conn:
                sources = conn.execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM books b "
                    " WHERE b.source_id=s.id AND b.status='queued') AS pending,"
                    " (SELECT COUNT(*) FROM books b WHERE b.source_id=s.id)"
                    " AS total FROM sources s WHERE s.enabled=1").fetchall()
            for s in sources:
                # 逐源隔离：一个馆异常（适配器 bug/未注册源/网络风暴）
                # 不得拖死整轮心跳，否则后续馆全部停摆且回收步骤被跳过。
                try:
                    _source_round(d, s, blocked_until)
                except Exception as e:
                    d.log(f"站点 {s['id']} 本轮异常: {e}", level="warn",
                          source=s["id"])
                    print(f"[scheduler] {s['id']}: 本轮异常 {e}", flush=True)
            # 超时/中断 running 回收（租约判活，同事务关闭旧 job 行）
            # 单独 try：即使上面某个馆/查询失败，回收也不能被跳过（审查 C P1-3）
            try:
                stuck = d.reap_stuck()
                if stuck:
                    d.log(f"回收 {stuck} 册中断 running → queued", level="warn",
                          source="scheduler")
                    print(f"[scheduler] 回收 {stuck} 册超时任务", flush=True)
            except Exception as e:
                print(f"[scheduler] 回收失败(本轮跳过): {e}", flush=True)

            # 事件日志裁剪（每 6 小时，保留最近 2000 条）
            if time.time() - last_prune > 6 * 3600:
                try:
                    with d.connect() as conn:
                        conn.execute("DELETE FROM events WHERE id < "
                                     "(SELECT COALESCE(MAX(id),0) - 2000 FROM events)")
                    last_prune = time.time()
                except sqlite3.Error as e:
                    print(f"[scheduler] 事件裁剪失败(忽略): {e}", flush=True)
        except KeyboardInterrupt:
            print("[scheduler] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[scheduler] 心跳异常(继续): {e}", flush=True)
            d.log(f"调度心跳异常: {e}", level="warn", source="scheduler")
        elapsed = time.monotonic() - round_start
        # 按固定节拍补足剩余时间；本轮超时就立刻开下一轮（不留负睡眠）
        _interruptible_sleep(max(1.0, HEARTBEAT_SEC - elapsed))
        print(f"[scheduler] 本轮耗时 {elapsed:.0f}s，"
              f"下轮间隔 {max(1.0, HEARTBEAT_SEC - elapsed):.0f}s", flush=True)


def _interruptible_sleep(seconds: float, step: float = 5.0) -> None:
    """分片 sleep：每 step 秒醒一次，让 SIGINT/SIGTERM 能在数秒内被处理。

    Python 主线程的 time.sleep 本身会被 SIGINT 打断，但 Docker 的 SIGTERM
    默认只置标志不会抛异常，长睡眠会让容器 stop 一直等到宽限期结束再 SIGKILL。
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(step, left))


def _source_round(d: DB, s, blocked_until: Dict[str, float]) -> None:
    """单个源的一轮心跳：目录发现 + 下载（配额由 jobs 账本跨进程共享）。"""
    # 1) 目录发现（direct 策略：空库首次收割 / 未完成续传 / 每周巡检）
    if s["meta_strategy"] == "direct" and s["catalog_url"] \
            and _stale(s["last_catalog_at"]):
        if time.time() < blocked_until.get(s["id"], 0):
            pass        # 限流冷却中，本轮跳过
        elif seed_catalog(d, s):
            until = time.time() + BLOCK_COOLDOWN_S
            blocked_until[s["id"]] = until
            # 落库：容器重启后仍记得冷却中（审查 C P1-2）
            try:
                with d.connect() as conn:
                    conn.execute(
                        "UPDATE sources SET last_blocked_at=? WHERE id=?",
                        (datetime.fromtimestamp(until, timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"), s["id"]))
            except sqlite3.Error as e:
                print(f"[scheduler] 写冷却态失败(忽略): {e}", flush=True)
        else:
            # 收割成功/未限流：清冷却态
            if blocked_until.pop(s["id"], None) is not None:
                try:
                    with d.connect() as conn:
                        conn.execute("UPDATE sources SET last_blocked_at='' "
                                     "WHERE id=?", (s["id"],))
                except sqlite3.Error:
                    pass
    # 2) 下载心跳（每源每小时 ≤ hourly_quota 册，DB 滑窗账本）
    if not s["pending"]:
        return
    tried, ok, hit = run_source_heartbeat(
        d, s["id"], s["hourly_quota"], s["quality"])
    print(f"[scheduler] {s['id']}: 尝试 {tried} 成功 {ok}"
          f"{'(配额尽)' if hit else ''}，剩 {s['pending'] - tried} 在队列",
          flush=True)


if __name__ == "__main__":
    main()
