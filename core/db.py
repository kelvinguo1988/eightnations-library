"""SQLite 存储层（WAL）。所有时间戳一律 UTC ISO8601。

状态机:
  discovered --approve--> queued --fetch--> running --> done
       |                                   |--> failed (可重试, attempt>=5 转 dead)
       +--ignore--> ignored
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT DEFAULT '',
  flag TEXT DEFAULT '',
  adapter TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  hourly_quota INTEGER NOT NULL DEFAULT 10,
  quality TEXT NOT NULL DEFAULT 'auto',      -- auto/pdf/orig/mid/thumb
  meta_strategy TEXT NOT NULL DEFAULT 'snapshot',
  catalog_url TEXT DEFAULT '',               -- direct 策略: 目录页 URL（自动收割）
  last_catalog_at TEXT DEFAULT '',           -- 上次收割时间（7 天巡检）
  last_blocked_at TEXT DEFAULT '',           -- 限流冷却到期时刻（跨重启持久）
  catalog_cursor INTEGER DEFAULT 1            -- 目录翻页游标（下次从第几页继续）
);
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_uid TEXT NOT NULL,
  title TEXT DEFAULT '',
  alt_title TEXT DEFAULT '',
  author TEXT DEFAULT '',
  era TEXT DEFAULT '',
  year_start INTEGER, year_end INTEGER,
  language TEXT DEFAULT '',
  subjects TEXT DEFAULT '[]',      -- JSON: 主题词/分类
  item_url TEXT DEFAULT '',
  cover_url TEXT DEFAULT '',
  cover_path TEXT DEFAULT '',
  collection TEXT DEFAULT '',
  volume_count INTEGER DEFAULT 0,
  page_count INTEGER DEFAULT 0,
  rights TEXT DEFAULT '',
  shelf_id TEXT DEFAULT '',
  pdf_urls TEXT DEFAULT '[]',      -- JSON: 每卷官方 PDF 直链
  files_json TEXT DEFAULT '[]',    -- JSON: 每卷页变体列表(IIIF 兜底用)
  raw_json TEXT DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'discovered',
  attempt INTEGER NOT NULL DEFAULT 0,
  needs_topup INTEGER NOT NULL DEFAULT 0,   -- 1=部分卷/页缺，等补齐（status 仍可为 done）
  last_error TEXT DEFAULT '',
  added_at TEXT NOT NULL,
  decided_at TEXT DEFAULT '',
  finished_at TEXT DEFAULT '',
  UNIQUE(source_id, source_uid)
);
CREATE INDEX IF NOT EXISTS idx_books_status ON books(status);
CREATE INDEX IF NOT EXISTS idx_books_source ON books(source_id, status);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL REFERENCES books(id),
  state TEXT NOT NULL,             -- running/done/failed
  quality TEXT DEFAULT '',
  pages_done INTEGER DEFAULT 0,
  pages_total INTEGER DEFAULT 0,
  bytes_done INTEGER DEFAULT 0,
  outputs TEXT DEFAULT '[]',
  last_error TEXT DEFAULT '',
  started_at TEXT NOT NULL,
  heartbeat_at TEXT DEFAULT '',    -- 下载进行中定期续约（租约判活）
  pid INTEGER DEFAULT 0,           -- 持有进程（同容器内 scheduler/web 可直接判活）
  finished_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reading_progress(
  book_id INTEGER PRIMARY KEY REFERENCES books(id),
  page INTEGER DEFAULT 1, scroll_pct REAL DEFAULT 0, zoom REAL DEFAULT 1,
  layout TEXT DEFAULT 'scroll', night INTEGER DEFAULT 0,
  theme TEXT DEFAULT 'light', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reading_daily(
  day TEXT PRIMARY KEY,                -- YYYY-MM-DD（UTC）
  seconds REAL DEFAULT 0,              -- 累计阅读秒数
  max_page INTEGER DEFAULT 0           -- 当日读到最深页
);
CREATE TABLE IF NOT EXISTS bookmarks(
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
  page INTEGER NOT NULL, note TEXT DEFAULT '', created_at TEXT,
  UNIQUE(book_id, page)
);
CREATE TABLE IF NOT EXISTS annotations(
  id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
  page INTEGER NOT NULL, kind TEXT NOT NULL,
  x0 REAL, y0 REAL, x1 REAL, y1 REAL,
  color TEXT DEFAULT '#ffe066', text TEXT DEFAULT '', created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_annotations_book ON annotations(book_id, page);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',   -- info/warn/error
  source TEXT DEFAULT '',
  book_id INTEGER,
  message TEXT NOT NULL
);
"""

# (id, name, country, flag, adapter, enabled, hourly_quota, quality, meta_strategy, catalog_url)
_DEFAULT_SOURCES = [
    ("loc", "美国国会图书馆", "美国", "🇺🇸", "loc", 1, 10, "auto", "snapshot", ""),
    ("na_jp", "日本国立公文書館", "日本", "🇯🇵", "na_jp", 1, 10, "auto", "direct",
     "https://www.digital.archives.go.jp/fonds/3611449?page=1"),
    ("ndl_jp", "日本国立国会图书馆", "日本", "🇯🇵", "ndl_jp", 0, 10, "auto", "direct", ""),
    ("bnf", "法国国家图书馆", "法国", "🇫🇷", "bnf", 0, 10, "auto", "direct",
     "https://catalogue.bnf.fr/api/SRU?version=1.2&operation=searchRetrieve"
     "&query=%28bib.digitized%20all%20%22freeAccess%22%29%20and%20%28bib.language"
     "%20all%20%22chi%22%29"),
]


# 同一本书的最大重试次数（失败计数）。放在 db 层是因为上限必须在
# claim_and_start 的 UPDATE ... AND attempt < ? 里原子生效；
# pipeline 只引用它做日志，不再各自定义（避免两处数字漂移）。
# 注意：sites/na_jp.py 里的 MAX_ATTEMPTS=8 是**块级**重试，与此无关。
MAX_ATTEMPTS = 5


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    """小而直接的 sqlite 封装：连接即用，写操作加锁。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _immediate(self):
        """显式写事务（BEGIN IMMEDIATE）：跨进程互斥，供认领/回收等
        读-改-写序列使用，避免两进程同时走 INSERT 分支的竞态。

        回滚保护（审查 A P1-5）：只有 BEGIN **成功之后**才回滚，且回滚本身
        出错绝不再抛——否则 "BEGIN IMMEDIATE" 因锁竞争失败时，except 分支里
        的 ROLLBACK 会抛 "cannot rollback - no transaction is active"，把真正的
        "database is locked" 覆盖掉，运维按日志根本查不到真因。
        """
        conn = self.connect()
        conn.isolation_level = None
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            # BaseException：KeyboardInterrupt/SystemExit 也必须回滚后再抛，
            # 但同样不能让回滚失败掩盖原始异常。
            if began:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass        # 原始异常优先，回滚失败只是"没有事务可回滚"
            raise
        finally:
            conn.close()

    def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._lock, self.connect() as conn:
            conn.executescript(_SCHEMA)
            # 老库迁移：补新列（新库建表时已含，ALTER 跳过）
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
            if "catalog_url" not in cols:
                conn.execute("ALTER TABLE sources ADD COLUMN catalog_url TEXT DEFAULT ''")
            if "last_catalog_at" not in cols:
                conn.execute("ALTER TABLE sources ADD COLUMN last_catalog_at TEXT DEFAULT ''")
            if "last_blocked_at" not in cols:
                # 老库补列：冷却态持久化（审查 C P1-2）。默认空 = 未限流，
                # 与既有行为一致，不会让任何老数据被"冷却"。
                conn.execute("ALTER TABLE sources ADD COLUMN "
                             "last_blocked_at TEXT DEFAULT ''")
            if "catalog_cursor" not in cols:
                # 老库补列：目录翻页游标（审查 C P0-4 配套）。默认 1 = 从头，
                # 与旧行为一致（旧实现每轮都从第 1 页扫）。
                conn.execute("ALTER TABLE sources ADD COLUMN "
                             "catalog_cursor INTEGER DEFAULT 1")
            bcols = {r["name"] for r in conn.execute("PRAGMA table_info(books)")}
            if "subjects" not in bcols:
                conn.execute("ALTER TABLE books ADD COLUMN subjects TEXT DEFAULT '[]'")
            if "favorite" not in bcols:
                conn.execute("ALTER TABLE books ADD COLUMN favorite INTEGER DEFAULT 0")
            if "needs_topup" not in bcols:
                # 老库补列：默认 0 = 不欠账（审查 A P1-2 的补齐标记，
                # 不动既有 done 数据的语义，48 册历史数据行为完全不变）
                conn.execute("ALTER TABLE books ADD COLUMN "
                             "needs_topup INTEGER NOT NULL DEFAULT 0")
            pcols = {r["name"] for r in conn.execute("PRAGMA table_info(reading_progress)")}
            if pcols and "theme" not in pcols:
                conn.execute("ALTER TABLE reading_progress ADD COLUMN theme TEXT DEFAULT 'light'")
            jcols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
            if "heartbeat_at" not in jcols:
                conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT DEFAULT ''")
            if "pid" not in jcols:
                conn.execute("ALTER TABLE jobs ADD COLUMN pid INTEGER DEFAULT 0")
            for row in _DEFAULT_SOURCES:
                conn.execute(
                    "INSERT OR IGNORE INTO sources(id,name,country,flag,adapter,"
                    "enabled,hourly_quota,quality,meta_strategy,catalog_url) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", row)
            # 老库回填 na_jp 默认目录
            conn.execute(
                "UPDATE sources SET catalog_url=? "
                "WHERE id='na_jp' AND (catalog_url IS NULL OR catalog_url='')",
                (_DEFAULT_SOURCES[1][9],))

    def toggle_favorite(self, book_id: int) -> Optional[bool]:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT favorite FROM books WHERE id=?",
                               (book_id,)).fetchone()
            if not row:
                return None
            val = 0 if row["favorite"] else 1
            conn.execute("UPDATE books SET favorite=? WHERE id=?", (val, book_id))
            return bool(val)

    def get_progress(self, book_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM reading_progress WHERE book_id=?",
                (book_id,)).fetchone()

    def save_progress(self, book_id: int, page: int, scroll_pct: float,
                      zoom: float, layout: str, theme: str = "light") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO reading_progress(book_id,page,scroll_pct,zoom,layout,"
                "night,theme,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET page=excluded.page,"
                "scroll_pct=excluded.scroll_pct,zoom=excluded.zoom,"
                "layout=excluded.layout,night=excluded.night,theme=excluded.theme,"
                "updated_at=excluded.updated_at",
                (book_id, page, scroll_pct, zoom, layout,
                 int(theme == "night"), theme, utcnow()))

    def reading_tick(self, book_id: int, seconds: float, page: int) -> None:
        """阅读心跳：按天累计秒数与最深页（书架"本周阅读"统计用）。"""
        day = utcnow()[:10]
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO reading_daily(day,seconds,max_page) VALUES(?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds,"
                "max_page=MAX(max_page,excluded.max_page)",
                (day, seconds, page))

    def week_minutes(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(seconds),0) s FROM reading_daily "
                "WHERE day >= date('now','-6 days')").fetchone()
        return int((row["s"] or 0) / 60)

    # ---- 标注 ----
    def list_annotations(self, book_id: int) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM annotations WHERE book_id=? ORDER BY page, id",
                (book_id,)).fetchall()

    def add_annotation(self, book_id: int, page: int, kind: str,
                       x0: float, y0: float, x1: float, y1: float,
                       color: str, text: str) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO annotations(book_id,page,kind,x0,y0,x1,y1,color,"
                "text,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (book_id, page, kind, x0, y0, x1, y1, color, text[:500],
                 utcnow()))
            return int(cur.lastrowid)

    def update_annotation(self, ann_id: int, kind: str, color: str,
                          text: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE annotations SET kind=?,color=?,text=? WHERE id=?",
                         (kind, color, text[:500], ann_id))

    def delete_annotation(self, ann_id: int) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM annotations WHERE id=?", (ann_id,))

    def list_bookmarks(self, book_id: int) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM bookmarks WHERE book_id=? ORDER BY page",
                (book_id,)).fetchall()

    def add_bookmark(self, book_id: int, page: int, note: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bookmarks(book_id,page,note,created_at) "
                "VALUES(?,?,?,?)", (book_id, page, note, utcnow()))

    def delete_bookmark(self, book_id: int, page: int) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("DELETE FROM bookmarks WHERE book_id=? AND page=?",
                         (book_id, page))

    def set_catalog_time(self, source_id: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE sources SET last_catalog_at=? WHERE id=?",
                         (utcnow(), source_id))
        self.set_catalog_cursor(source_id, 1)     # 封存：下轮从头巡检

    def get_catalog_cursor(self, source_id: str) -> int:
        """目录翻页游标（下次从第几页继续）。老库/缺列时回落 1。"""
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT catalog_cursor FROM sources "
                                   "WHERE id=?", (source_id,)).fetchone()
            v = int((row["catalog_cursor"] if row else 1) or 1)
            return max(1, v)
        except (sqlite3.Error, TypeError, ValueError):
            return 1

    def set_catalog_cursor(self, source_id: str, page: int) -> None:
        """持久化翻页游标：容器重启后不必从第 1 页重扫（审查 C P0-4）。

        这是"翻到最后一页"这条长路径的唯一可靠进度载体——旧实现只靠
        last_catalog_at 的 7 天过期信号，所以 5268 件的目录每 7 天只处理
        预算内的前 40 件，永远跑不完。
        """
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE sources SET catalog_cursor=? WHERE id=?",
                         (max(1, int(page)), source_id))

    # ---- books ----
    @staticmethod
    def _check_path_segments(source_uid: str, collection: str) -> None:
        """入库即拒绝会成为路径穿越的元素（这些值后续直接做落盘路径段）。

        collection 允许为空（落盘归入 misc 目录）；source_uid 必须非空。
        """
        def _illegal(v: str) -> bool:
            return (v in (".", "..") or "/" in v or "\\" in v or "\x00" in v)
        if not source_uid or _illegal(source_uid):
            raise ValueError(f"非法 source_uid: {source_uid!r}")
        if collection and _illegal(collection):
            raise ValueError(f"非法 collection: {collection!r}")

    def upsert_book(self, source_id: str, meta: Dict[str, Any]) -> bool:
        """按 (source_id, source_uid) 插入或更新元数据；保留状态/决策字段。

        返回 True 表示是新插入（即"新书"）。跨进程并发安全：写事务内
        SELECT→UPDATE/INSERT，唯一约束冲突（另一进程刚插入）回退为更新。
        """
        self._check_path_segments(meta["source_uid"],
                                  meta.get("collection", ""))
        cols = {
            "title": meta.get("title", ""), "alt_title": meta.get("alt_title", ""),
            "author": meta.get("author", ""), "era": meta.get("era", ""),
            "year_start": meta.get("year_start"), "year_end": meta.get("year_end"),
            "language": meta.get("language", ""),
            "subjects": json.dumps(meta.get("subjects") or [], ensure_ascii=False),
            "item_url": meta.get("item_url", ""),
            "cover_url": meta.get("cover_url", ""), "collection": meta.get("collection", ""),
            "volume_count": meta.get("volume_count", 0),
            "page_count": meta.get("page_count", 0), "rights": meta.get("rights", ""),
            "shelf_id": meta.get("shelf_id", ""),
            "pdf_urls": json.dumps(meta.get("pdf_urls") or [], ensure_ascii=False),
            "files_json": json.dumps(meta.get("page_files") or [], ensure_ascii=False),
            "raw_json": json.dumps(meta.get("raw") or {}, ensure_ascii=False),
        }

        def _update_or_insert(conn) -> bool:
            row = conn.execute(
                "SELECT id, subjects, pdf_urls, files_json FROM books "
                "WHERE source_id=? AND source_uid=?",
                (source_id, meta["source_uid"])).fetchone()
            if row:
                # 合并保护：新快照缺某字段(空值)时不覆盖库中已有数据
                # （如早期紧凑快照无 subject、集合级无逐页清单）
                for k in ("subjects", "pdf_urls", "files_json"):
                    if cols[k] in ("[]", "") and (row[k] or "[]") not in ("[]", ""):
                        cols[k] = row[k]
                sets = ",".join(f"{k}=?" for k in cols)
                conn.execute(f"UPDATE books SET {sets} WHERE id=?",
                             (*cols.values(), row["id"]))
                return False
            conn.execute(
                "INSERT INTO books(source_id,source_uid,added_at," + ",".join(cols) + ") "
                "VALUES(?,?,?" + ",?" * len(cols) + ")",
                (source_id, meta["source_uid"], utcnow(), *cols.values()))
            return True

        with self._lock, self._immediate() as conn:
            try:
                return _update_or_insert(conn)
            except sqlite3.IntegrityError:
                # _immediate 已 ROLLBACK；等另一进程事务结束后重试即为更新分支
                with self._immediate() as retry:
                    return _update_or_insert(retry)

    def get_book(self, book_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()

    def find_book(self, source_id: str, source_uid: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM books WHERE source_id=? AND source_uid=?",
                (source_id, source_uid)).fetchone()

    def list_books(self, status: str = "", source_id: str = "",
                   collection: str = "", keyword: str = "", era: str = "",
                   subjects: str = "", favorite: bool = False,
                   limit: int = 200, offset: int = 0) -> List[sqlite3.Row]:
        where, args = self._book_filters(status, source_id, collection, keyword,
                                         era, subjects, favorite)
        sql = "SELECT * FROM books" + where + " ORDER BY id LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    @staticmethod
    def _book_filters(status: str = "", source_id: str = "",
                      collection: str = "", keyword: str = "",
                      era: str = "", subjects: str = "",
                      favorite: bool = False) -> tuple:
        sql, args = " WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if favorite:
            sql += " AND favorite=1"
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        if collection:
            sql += " AND collection=?"
            args.append(collection)
        if era:
            vals = [("" if e.strip().lower() in ("unknown", "未知") else e.strip())
                    for e in str(era).split(",") if e.strip()]
            if vals:
                sql += f" AND era IN ({','.join('?' * len(vals))})"
                args += vals
        if subjects:
            tags = [t.strip() for t in str(subjects).split(",") if t.strip()]
            if tags:
                cond = " OR ".join("subjects LIKE ?" for _ in tags)
                sql += f" AND ({cond})"
                args += [f'%"{t}"%' for t in tags]
        if keyword:
            sql += " AND (title LIKE ? OR alt_title LIKE ? OR shelf_id LIKE ? OR subjects LIKE ?)"
            args += [f"%{keyword}%"] * 4
        return sql, args

    def count_books(self, status: str = "", source_id: str = "",
                    collection: str = "", keyword: str = "", era: str = "",
                    subjects: str = "", favorite: bool = False) -> int:
        where, args = self._book_filters(status, source_id, collection, keyword,
                                         era, subjects, favorite)
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM books" + where, args).fetchone()
            return row["n"]

    def facets(self, source_id: str = "") -> Dict[str, List[str]]:
        """筛选下拉候选：专藏 / 朝代。"""
        conds, args = "", []
        if source_id:
            conds, args = " WHERE source_id=?", [source_id]
        with self.connect() as conn:
            cols = conn.execute("SELECT DISTINCT collection FROM books" + conds +
                                " ORDER BY collection", args).fetchall()
            eras = conn.execute("SELECT DISTINCT era FROM books" + conds +
                                " ORDER BY era", args).fetchall()
            tags: Dict[str, int] = {}
            for r in conn.execute("SELECT subjects FROM books" + conds, args):
                try:
                    for t in json.loads(r["subjects"] or "[]"):
                        if t:
                            tags[t] = tags.get(t, 0) + 1
                except Exception:
                    pass
        return {"collections": [c["collection"] for c in cols if c["collection"]],
                "eras": [e["era"] for e in eras if e["era"]],
                "tags": [t for t, _ in sorted(tags.items(),
                                              key=lambda kv: -kv[1])]}

    def count_by_status(self, source_id: str = "") -> Dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM books"
        args: List[Any] = []
        if source_id:
            sql += " WHERE source_id=?"
            args.append(source_id)
        sql += " GROUP BY status"
        with self.connect() as conn:
            return {r["status"]: r["n"] for r in conn.execute(sql, args)}

    def set_status(self, book_id: int, status: str, error: str = "",
                   bump_attempt: bool = False,
                   reset_attempt: bool = False) -> None:
        """改状态（人工决策/管理入口）。

        reset_attempt=True：把 attempt 归零（人工"重试"语义）。
        审查 A P0-3：attempt 上限已下沉到 claim_and_start 的 SQL，dead 需要
        显式 force 才能再认领；人工重试走 set_status(queued, reset_attempt=True)
        把这本书当"新任务"，否则它永远卡在上限之外。
        """
        sets = ["status=?", "last_error=?"]
        args: List[Any] = [status, error]
        if status in ("queued", "ignored"):
            sets.append("decided_at=?")
            args.append(utcnow())
        if status == "done":
            sets.append("finished_at=?")
            args.append(utcnow())
        if bump_attempt:
            sets.append("attempt=attempt+1")
        if reset_attempt:
            sets.append("attempt=0")     # 人工重试：从零开始计次
        args.append(book_id)
        with self._lock, self.connect() as conn:
            conn.execute(f"UPDATE books SET {','.join(sets)} WHERE id=?", args)

    def claim_book(self, book_id: int) -> bool:
        """原子认领：仅当仍处于 queued/failed/dead 时置为 running。

        防止调度器与 Web 手动触发并发抓同一本书（双写同一 .part 会损坏文件）。
        返回 False 表示已被其他执行方认领。
        """
        job_id, quota_full, claimed = self.claim_and_start(book_id, "auto")
        return claimed

    def claim_and_start(self, book_id: int, quality: str,
                        quota_limit: Optional[int] = None,
                        force: bool = False) -> tuple:
        """认领 + 建 job 同一写事务（不留下"running 但无 job"的孤儿窗口）。

        每小时配额以 jobs.started_at 为账本（跨进程共享，落库持久），
        在认领事务内计数，检查与消费原子完成。

        认领准入（审查 A P0-3）：
          * queued / failed：可认领；
          * dead：**只有在 force=True 时**才可认领。dead 是"已放弃，需人工介入"
            的语义；修前它与 failed 一样随时可认领，配合 Web「重试」可以无限
            重跑（实测 25 次、attempt 25），每次都消耗该馆每小时配额。
          * attempt 已达上限的书一律不再认领（上限判断下沉到 UPDATE 的 WHERE，
            与状态迁移在同一原子语句里完成，跨进程也安全）。

        返回 (job_id, quota_full, claimed)。第 4 个值 attempts 通过
        `claim_and_start(...)` 的返回元组保持 3 元不变（兼容旧调用），
        认领后的真实 attempt 请用 `book_attempt(book_id)` 读取。
        """
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        allowed = ("queued", "failed", "dead") if force else ("queued", "failed")
        with self._lock, self._immediate() as conn:
            row = conn.execute(
                "SELECT status, source_id, needs_topup FROM books WHERE id=?",
                (book_id,)).fetchone()
            if not row:
                return None, False, False
            # needs_topup=1 的 done 书是"已归档但欠卷"：允许认领以补齐
            # （审查 A P1-2）。它的 status 仍是 done，所以要与 allowed 分开判，
            # 且不受 attempt 上限约束（补齐不是"重试失败的书"）。
            topup = bool(row["needs_topup"]) and row["status"] == "done"
            if row["status"] not in allowed and not topup:
                return None, False, False
            if quota_limit is not None:
                used = conn.execute(
                    "SELECT COUNT(*) n FROM jobs j JOIN books b ON b.id=j.book_id "
                    "WHERE b.source_id=? AND j.started_at>=?",
                    (row["source_id"], cutoff)).fetchone()["n"]
                if used >= quota_limit:
                    return None, True, False
            # attempt 上限写进 WHERE：跨进程原子，且"状态可认领 + 未超上限"一次判定。
            # 补齐路径（done+needs_topup）不递增 attempt、不设上限。
            if topup:
                cur = conn.execute(
                    "UPDATE books SET status='running', needs_topup=0 "
                    "WHERE id=? AND status='done' AND needs_topup=1", (book_id,))
            else:
                cur = conn.execute(
                    "UPDATE books SET status='running', attempt=attempt+1 "
                    "WHERE id=? AND status IN (%s) AND attempt < ?"
                    % ",".join("?" * len(allowed)),
                    (book_id, *allowed, MAX_ATTEMPTS))
            if not cur.rowcount:
                return None, False, False
            job = conn.execute(
                "INSERT INTO jobs(book_id,state,quality,started_at,pid) "
                "VALUES(?,?,?,?,?)",
                (book_id, "running", quality, utcnow(), os.getpid()))
            return int(job.lastrowid), False, True

    def book_attempt(self, book_id: int) -> int:
        """读认领后的真实 attempt（失败计数用它，不要用认领前的 row 快照）。"""
        with self.connect() as conn:
            row = conn.execute("SELECT attempt FROM books WHERE id=?",
                               (book_id,)).fetchone()
        return int(row["attempt"]) if row else 0

    def job_heartbeat(self, job_id: int) -> None:
        """下载进行中续约租约（reap 以此判活）。"""
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=? AND state='running'",
                         (utcnow(), job_id))

    def update_download_info(self, book_id: int, cover_path: str,
                             page_count: int, pages_done: int = 0) -> None:
        """写封面路径与页数。

        page_count 保留"更大的那个"（审查 C P1 / A C4）：快照里的
        catalog page_count 是整本的权威值，下载层可能因弱成功只拿到一部分，
        直接覆盖会让"这本书到底多少页"变小。只有本次确实拿到更多页时才更新。
        """
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE books SET cover_path=?, "
                "page_count=MAX(COALESCE(page_count,0), ?) WHERE id=?",
                (cover_path, max(page_count, pages_done, 0), book_id))

    def queued_books(self, source_id: str, limit: int,
                     include_topup: bool = True) -> List[sqlite3.Row]:
        """取本轮要下载的书：先正常 queued，再补位"部分完成待补齐"的书。

        审查 A P1-2：部分卷成功后旧实现直接置 done 并离开队列，缺卷永远
        没人补。修法**不新增状态**（done 的语义/UI/查询口径全部保持），
        而是给 needs_topup=1 的 done 书保留一条"队尾补位"通道：
          * 排在真正 queued 之后，不抢占新书的配额；
          * 每个源每轮最多补 limit//2 册（避免欠账书把队列堵死）；
          * 补齐成功后 needs_topup 归 0，自然退出队列。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM books WHERE source_id=? AND status='queued' "
                "ORDER BY id LIMIT ?", (source_id, limit)).fetchall()
            if not include_topup or len(rows) >= limit:
                return rows
            room = min(limit - len(rows), max(1, limit // 2))
            topup = conn.execute(
                "SELECT * FROM books WHERE source_id=? AND status='done' "
                "AND needs_topup=1 ORDER BY finished_at, id LIMIT ?",
                (source_id, room)).fetchall()
            return list(rows) + list(topup)

    def record_done(self, book_id: int, job_id: int, pages: int, total: int,
                    bytes_done: int, outputs: List[str]) -> None:
        """全部卷/页齐全：done + 清欠账标记（同一事务，避免 states 不一致）。"""
        with self._lock, self._immediate() as conn:
            conn.execute(
                "UPDATE jobs SET state='done',pages_done=?,pages_total=?,"
                "bytes_done=?,outputs=?,last_error='',finished_at=? WHERE id=?",
                (pages, total, bytes_done,
                 json.dumps(outputs, ensure_ascii=False), utcnow(), job_id))
            conn.execute(
                "UPDATE books SET status='done',needs_topup=0,last_error='',"
                "finished_at=? WHERE id=?", (utcnow(), book_id))

    def record_partial(self, book_id: int, job_id: int, pages: int, total: int,
                       bytes_done: int, outputs: List[str],
                       missing: List[str], error: str = "") -> None:
        """部分完成：**记 done（已归档部分可读）+ needs_topup=1（欠账待补）**。

        为什么不新增 partial 状态：
          * done 被 web/app.py 书架、/explore、favorite、doctor 对账等 8 处
            直接按字符串查询；新增状态会让这些已归档的书在 UI 上"消失"，
            必须同步改 web/ 才能不掉数据，改动面是现在的 3 倍；
          * 老库 42 册 done 的语义（"可阅读的已归档书"）与"部分完成"其实一致：
            用户需要的是**能读 + 之后会自己补齐**，而不是两种互斥状态。
        所以用"状态不变 + 欠账标记"表达，补齐成功后 needs_topup 归 0。
        attempt 不递增：部分成功不是失败，不该消耗重试次数。
        """
        note = "部分完成待补: " + ("; ".join(missing) if missing else (error or "未知"))
        with self._lock, self._immediate() as conn:
            conn.execute(
                "UPDATE jobs SET state='done',pages_done=?,pages_total=?,"
                "bytes_done=?,outputs=?,last_error=?,finished_at=? WHERE id=?",
                (pages, total, bytes_done,
                 json.dumps(outputs, ensure_ascii=False), note[:500],
                 utcnow(), job_id))
            conn.execute(
                "UPDATE books SET status='done',needs_topup=1,last_error=? "
                "WHERE id=?", (note[:500], book_id))

    # ---- jobs / events ----
    def start_job(self, book_id: int, quality: str) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(book_id,state,quality,started_at,pid) "
                "VALUES(?,'running',?,?,?)",
                (book_id, quality, utcnow(), os.getpid()))
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, state: str, pages: int, total: int,
                   bytes_done: int, outputs: List[str], error: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state=?,pages_done=?,pages_total=?,bytes_done=?,"
                "outputs=?,last_error=?,finished_at=? WHERE id=?",
                (state, pages, total, bytes_done,
                 json.dumps(outputs, ensure_ascii=False), error, utcnow(), job_id))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """同容器/同主机判活（scheduler 与 web 共容器，pid 可直接探测）。
        pid<=0 为旧数据未知持有者 → 视作存活，交给超时规则。"""
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def reap_stuck(self, stale_minutes: int = 60) -> int:
        """回收中断的下载（租约模型，替代原"无条件 running→queued"）：

        * running job 的持有进程已退出 → 立即回收（进程重启后无 PID 复用窗口）；
        * 或超过 stale_minutes 未续约（旧数据/同机 PID 被复用导致的假活）→ 回收；
        * 回收时同一事务内关闭 jobs 行——否则旧 job 永久 running，书被重新
          认领后每个心跳又会被超时规则误回收，与真正执行方双写。
        返回回收册数。
        """
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(minutes=stale_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = utcnow()
        with self._lock, self._immediate() as conn:
            jobs = conn.execute(
                "SELECT id, book_id, pid, "
                " COALESCE(NULLIF(heartbeat_at,''),started_at) AS last_beat "
                "FROM jobs WHERE state='running'").fetchall()
            reaped_books = []
            for j in jobs:
                if self._pid_alive(j["pid"]) and j["last_beat"] >= cutoff:
                    continue
                conn.execute(
                    "UPDATE jobs SET state='failed', last_error='执行方中断，租约回收', "
                    "finished_at=? WHERE id=?", (now, j["id"]))
                reaped_books.append(j["book_id"])
            n = 0
            for book_id in reaped_books:
                cur = conn.execute(
                    "UPDATE books SET status='queued', last_error='下载中断回收' "
                    "WHERE id=? AND status='running' AND NOT EXISTS "
                    "(SELECT 1 FROM jobs WHERE book_id=? AND state='running')",
                    (book_id, book_id))
                n += cur.rowcount
            return n

    def log(self, message: str, level: str = "info", source: str = "",
            book_id: Optional[int] = None) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO events(ts,level,source,book_id,message) VALUES(?,?,?,?,?)",
                (utcnow(), level, source, book_id, message))
