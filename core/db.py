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
  last_catalog_at TEXT DEFAULT ''            -- 上次收割时间（7 天巡检）
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
        读-改-写序列使用，避免两进程同时走 INSERT 分支的竞态。"""
        conn = self.connect()
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
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
            bcols = {r["name"] for r in conn.execute("PRAGMA table_info(books)")}
            if "subjects" not in bcols:
                conn.execute("ALTER TABLE books ADD COLUMN subjects TEXT DEFAULT '[]'")
            if "favorite" not in bcols:
                conn.execute("ALTER TABLE books ADD COLUMN favorite INTEGER DEFAULT 0")
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
                   bump_attempt: bool = False) -> None:
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
                        quota_limit: Optional[int] = None) -> tuple:
        """认领 + 建 job 同一写事务（不留下"running 但无 job"的孤儿窗口）。

        每小时配额以 jobs.started_at 为账本（跨进程共享，落库持久），
        在认领事务内计数，检查与消费原子完成。

        返回 (job_id, quota_full, claimed)。
        """
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock, self._immediate() as conn:
            row = conn.execute(
                "SELECT status, source_id FROM books WHERE id=?",
                (book_id,)).fetchone()
            if not row or row["status"] not in ("queued", "failed", "dead"):
                return None, False, False
            if quota_limit is not None:
                used = conn.execute(
                    "SELECT COUNT(*) n FROM jobs j JOIN books b ON b.id=j.book_id "
                    "WHERE b.source_id=? AND j.started_at>=?",
                    (row["source_id"], cutoff)).fetchone()["n"]
                if used >= quota_limit:
                    return None, True, False
            cur = conn.execute(
                "UPDATE books SET status='running', attempt=attempt+1 "
                "WHERE id=? AND status IN ('queued','failed','dead')",
                (book_id,))
            if not cur.rowcount:
                return None, False, False
            job = conn.execute(
                "INSERT INTO jobs(book_id,state,quality,started_at,pid) "
                "VALUES(?,?,?,?,?)",
                (book_id, "running", quality, utcnow(), os.getpid()))
            return int(job.lastrowid), False, True

    def job_heartbeat(self, job_id: int) -> None:
        """下载进行中续约租约（reap 以此判活）。"""
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=? AND state='running'",
                         (utcnow(), job_id))

    def update_download_info(self, book_id: int, cover_path: str,
                             page_count: int) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE books SET cover_path=?, page_count=? WHERE id=?",
                         (cover_path, page_count, book_id))

    def queued_books(self, source_id: str, limit: int) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM books WHERE source_id=? AND status='queued' "
                "ORDER BY id LIMIT ?", (source_id, limit)).fetchall()

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
