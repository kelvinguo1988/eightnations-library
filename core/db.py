"""SQLite 存储层（WAL）。所有时间戳一律 UTC ISO8601。

状态机:
  discovered --approve--> queued --fetch--> running --> done
       |                                   |--> failed (可重试, attempt>=5 转 dead)
       +--ignore--> ignored
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
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
  finished_at TEXT DEFAULT ''
);
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

    def init(self) -> None:
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

    def set_catalog_time(self, source_id: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE sources SET last_catalog_at=? WHERE id=?",
                         (utcnow(), source_id))

    # ---- books ----
    def upsert_book(self, source_id: str, meta: Dict[str, Any]) -> bool:
        """按 (source_id, source_uid) 插入或更新元数据；保留状态/决策字段。

        返回 True 表示是新插入（即"新书"）。
        """
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
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "SELECT id, subjects, pdf_urls, files_json FROM books "
                "WHERE source_id=? AND source_uid=?",
                (source_id, meta["source_uid"]))
            row = cur.fetchone()
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
                   limit: int = 200, offset: int = 0) -> List[sqlite3.Row]:
        where, args = self._book_filters(status, source_id, collection, keyword, era)
        sql = "SELECT * FROM books" + where + " ORDER BY id LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self.connect() as conn:
            return conn.execute(sql, args).fetchall()

    @staticmethod
    def _book_filters(status: str = "", source_id: str = "",
                      collection: str = "", keyword: str = "",
                      era: str = "") -> tuple:
        sql, args = " WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if source_id:
            sql += " AND source_id=?"
            args.append(source_id)
        if collection:
            sql += " AND collection=?"
            args.append(collection)
        if era:
            sql += " AND era=?"
            args.append(era)
        if keyword:
            sql += " AND (title LIKE ? OR alt_title LIKE ? OR shelf_id LIKE ? OR subjects LIKE ?)"
            args += [f"%{keyword}%"] * 4
        return sql, args

    def count_books(self, status: str = "", source_id: str = "",
                    collection: str = "", keyword: str = "", era: str = "") -> int:
        where, args = self._book_filters(status, source_id, collection, keyword, era)
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
        return {"collections": [c["collection"] for c in cols if c["collection"]],
                "eras": [e["era"] for e in eras if e["era"]]}

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
                "INSERT INTO jobs(book_id,state,quality,started_at) "
                "VALUES(?,'running',?,?)", (book_id, quality, utcnow()))
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, state: str, pages: int, total: int,
                   bytes_done: int, outputs: List[str], error: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET state=?,pages_done=?,pages_total=?,bytes_done=?,"
                "outputs=?,last_error=?,finished_at=? WHERE id=?",
                (state, pages, total, bytes_done,
                 json.dumps(outputs, ensure_ascii=False), error, utcnow(), job_id))

    def log(self, message: str, level: str = "info", source: str = "",
            book_id: Optional[int] = None) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO events(ts,level,source,book_id,message) VALUES(?,?,?,?,?)",
                (utcnow(), level, source, book_id, message))
