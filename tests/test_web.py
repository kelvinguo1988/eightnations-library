"""域测试：Web 层（FastAPI TestClient，离线）。

覆盖批 1 的 6 项修复（docs/review/FIX-BATCH1.md）与 B-web.md §9 建议的
零依赖结构断言。所有用例走 tmp 数据目录，不碰宿主 data/。

契约（批 1 定稿，测试按此写）:
  * 页面全部 200（空库也能渲染）
  * 非法/空/非对象 JSON → 400（不得 500）
  * /api/health：正常 200 {ok:true,db:true}；DB 不可用 → 503 {ok:false}
  * /read?p=：越界 clamp 到 meta.json.pages 或 books.page_count；未知上限用
    PAGE_MAX 宽松兜底；p<1 → 1
  * theme=sepia 能存住；非法 theme 回落 light
  * 标注正文超长 → 截断到 ANN_TEXT_MAX（200），页码越界 → 400
"""
import json
import os

import pytest

from conftest import write_pdf

pytestmark = [pytest.mark.web, pytest.mark.guard]

# 批 1 之前"曾 500"的 6 个接口（7 个调用点）
JSON_ENDPOINTS = [
    ("post", "/api/bookmarks"),
    ("post", "/api/annotations"),
    ("patch", "/api/annotations/999"),
    ("post", "/api/reading-tick"),
    ("post", "/api/review"),
    ("post", "/api/retry"),
    ("post", "/api/progress/1"),
]


def _db(client):
    return client.app_module.get_db()


def _seed_book(client, uid="web-uid-1", collection="test-set",
               page_count=120, pdf=True, pages_in_meta=None):
    """插一本 done 且有实体 PDF 的书；返回 (book_id, dest_dir)。"""
    from core.pipeline import _dest_dir
    d = _db(client)
    d.upsert_book("loc", {"source_uid": uid, "collection": collection,
                          "title": "測試書名", "alt_title": "測試書名",
                          "page_count": page_count})
    row = d.find_book("loc", uid)
    dest = _dest_dir("loc", collection, uid)
    os.makedirs(dest, exist_ok=True)
    if pdf:
        write_pdf(os.path.join(dest, "book.pdf"), 2)
    if pages_in_meta is not None:
        with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"pages": pages_in_meta, "source": "loc",
                       "files": [{"path": "book.pdf"}]}, f)
    d.set_status(row["id"], "done")
    return row["id"], dest


# =====================================================================
# 1. 页面可渲染
# =====================================================================
@pytest.mark.parametrize("path", ["/", "/explore", "/review", "/jobs",
                                  "/settings"])
def test_pages_render_200(web_client, path):
    r = web_client.get(path)
    assert r.status_code == 200, (path, r.text[:300])
    assert "<html" in r.text.lower()


def test_book_detail_and_reader_render(web_client):
    bid, _ = _seed_book(web_client)
    assert web_client.get(f"/books/{bid}").status_code == 200
    assert web_client.get(f"/read/{bid}").status_code == 200
    assert web_client.get("/books/999999").status_code == 404
    assert web_client.get("/read/999999").status_code == 404


def test_reader_without_pdf_is_404(web_client):
    bid, _ = _seed_book(web_client, uid="nopdf", pdf=False)
    r = web_client.get(f"/read/{bid}")
    assert r.status_code == 404


# =====================================================================
# 2. 非法 JSON → 400（批 1 第 4 项）
# =====================================================================
@pytest.mark.parametrize("method,path", JSON_ENDPOINTS)
def test_invalid_json_body_returns_400(web_client, method, path):
    fn = getattr(web_client, method)
    for bad in (b"{not json", b"", b"[1,2,3]", b'"x"', b"null"):
        r = fn(path, content=bad,
               headers={"Content-Type": "application/json"})
        assert r.status_code == 400, (method, path, bad, r.status_code)
        assert r.status_code < 500


def test_valid_json_still_works_after_guard(web_client):
    bid, _ = _seed_book(web_client)
    r = web_client.post("/api/bookmarks",
                        json={"book_id": bid, "page": 1, "note": "ok"})
    assert r.status_code == 200
    assert r.json().get("ok") is True


# =====================================================================
# 3. /api/health：200 / 503（批 1 第 5 项）
# =====================================================================
def test_health_ok(web_client):
    r = web_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["db"] is True


def test_health_503_when_db_unavailable(web_client, monkeypatch):
    """DB 打不开时必须 503（Dockerfile HEALTHCHECK 靠非 2xx 判 unhealthy）。"""
    app_mod = web_client.app_module

    class _Broken:
        def connect(self):
            raise RuntimeError("unable to open database file")

        def init(self):
            raise RuntimeError("unable to open database file")

    monkeypatch.setattr(app_mod, "_DB", _Broken())
    r = web_client.get("/api/health")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["ok"] is False and body["db"] is False
    assert "error" in body


# =====================================================================
# 4. /read?p= clamp（批 1 第 6 项）
# =====================================================================
def test_read_page_clamped_to_meta_pages(web_client):
    bid, _ = _seed_book(web_client, uid="clamp-meta", page_count=0,
                        pages_in_meta=120)
    html = web_client.get(f"/read/{bid}?p=99999999").text
    assert 'data-page="120"' in html, html[:500]
    html = web_client.get(f"/read/{bid}?p=-999").text
    assert 'data-page="1"' in html


def test_read_page_clamped_to_book_page_count(web_client):
    """没有 meta.json 时用 books.page_count 兜底（批 1 的真实服务验证路径）。"""
    bid, _ = _seed_book(web_client, uid="clamp-pc", page_count=300,
                        pages_in_meta=None)
    html = web_client.get(f"/read/{bid}?p=99999999").text
    assert 'data-page="300"' in html


def test_read_page_unknown_upper_bound_uses_page_max(web_client):
    """上限未知时不得把合法页码压成 1（PAGE_MAX 宽松兜底）。"""
    app_mod = web_client.app_module
    bid, _ = _seed_book(web_client, uid="clamp-unknown", page_count=0,
                        pages_in_meta=None)
    html = web_client.get(f"/read/{bid}?p=12345").text
    assert 'data-page="12345"' in html, html[:400]
    assert app_mod.PAGE_MAX == 100000


def test_read_page_normal_value_passes(web_client):
    bid, _ = _seed_book(web_client, uid="clamp-ok", pages_in_meta=120)
    assert 'data-page="50"' in web_client.get(f"/read/{bid}?p=50").text


# =====================================================================
# 5. 主题白名单（批 1 第 2 项）
# =====================================================================
def _progress_row(client, bid):
    with _db(client).connect() as conn:
        return conn.execute("SELECT page, theme FROM reading_progress "
                            "WHERE book_id=?", (bid,)).fetchone()


@pytest.mark.parametrize("theme,expect", [("sepia", "sepia"),
                                          ("night", "night"),
                                          ("light", "light")])
def test_theme_persisted(web_client, theme, expect):
    bid, _ = _seed_book(web_client, uid=f"theme-{theme}")
    r = web_client.post(f"/api/progress/{bid}",
                        json={"page": 3, "scroll_pct": 0.1, "zoom": 1.0,
                              "layout": "scroll", "theme": theme})
    assert r.status_code == 200
    row = _progress_row(web_client, bid)
    assert row is not None and row["theme"] == expect


def test_illegal_theme_falls_back_to_light(web_client):
    bid, _ = _seed_book(web_client, uid="theme-evil")
    web_client.post(f"/api/progress/{bid}",
                    json={"page": 1, "theme": "evil;--x"})
    assert _progress_row(web_client, bid)["theme"] == "light"


def test_themes_whitelist_contains_all_reader_themes(web_client):
    """跨文件一致性：reader.js 循环的每个主题都必须在 THEMES 白名单里。"""
    import re
    app_mod = web_client.app_module
    js = (app_mod.BASE_DIR and
          open(os.path.join(app_mod.BASE_DIR, "web", "static", "reader.js"),
               encoding="utf-8").read())
    found = set()
    for m in re.finditer(r'\[([^\[\]]*)\]\.includes\(root\.dataset\.theme\)',
                         js):
        found |= set(re.findall(r'["\']([a-z]+)["\']', m.group(1)))
    for m in re.finditer(r'theme = \{([^}]*)\}\[theme\]', js):
        found |= set(re.findall(r'["\']?([a-z]+)["\']?\s*:', m.group(1)))
    assert "sepia" in found, f"reader.js 主题循环解析失败: {found}"
    missing = found - app_mod.THEMES
    assert not missing, f"reader.js 有但 THEMES 没有（会被静默降级）: {missing}"


# =====================================================================
# 6. 标注/书签输入上限（批 1 第 6 项）
# =====================================================================
def test_annotation_text_truncated_to_max(web_client):
    app_mod = web_client.app_module
    bid, _ = _seed_book(web_client, uid="ann-long")
    r = web_client.post("/api/annotations",
                        json={"book_id": bid, "page": 1, "text": "字" * 10000,
                              "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5})
    assert r.status_code == 200
    ann_id = r.json()["id"]
    d = _db(web_client)
    row = d.list_annotations(bid)[0]
    assert len(row["text"]) == app_mod.ANN_TEXT_MAX, len(row["text"])
    # PATCH 同样截断
    r = web_client.patch(f"/api/annotations/{ann_id}",
                         json={"text": "x" * 10000})
    assert r.status_code == 200
    assert len(d.list_annotations(bid)[0]["text"]) == app_mod.ANN_TEXT_MAX


@pytest.mark.parametrize("page", [0, -1, 2 ** 62])
def test_annotation_page_out_of_range_400(web_client, page):
    bid, _ = _seed_book(web_client, uid=f"ann-page-{page}")
    r = web_client.post("/api/annotations",
                        json={"book_id": bid, "page": page, "text": "x",
                              "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5})
    assert r.status_code == 400, (page, r.status_code)


@pytest.mark.parametrize("page", [0, -5, 2 ** 62])
def test_bookmark_page_out_of_range_400(web_client, page):
    bid, _ = _seed_book(web_client, uid=f"bm-page-{page}")
    r = web_client.post("/api/bookmarks", json={"book_id": bid, "page": page})
    assert r.status_code == 400, (page, r.status_code)


def test_progress_page_clamped_to_page_max(web_client):
    app_mod = web_client.app_module
    bid, _ = _seed_book(web_client, uid="prog-clamp")
    web_client.post(f"/api/progress/{bid}", json={"page": 2 ** 62})
    assert _progress_row(web_client, bid)["page"] == app_mod.PAGE_MAX


def test_annotation_tiny_selection_rejected(web_client):
    bid, _ = _seed_book(web_client, uid="ann-tiny")
    r = web_client.post("/api/annotations",
                        json={"book_id": bid, "page": 1, "text": "x",
                              "x0": 0.10, "y0": 0.10, "x1": 0.1001,
                              "y1": 0.10})
    assert r.status_code == 400


# =====================================================================
# 7. 审核 / 重试 语义
# =====================================================================
def test_review_action_validation(web_client):
    assert web_client.post("/api/review", json={}).status_code == 400
    assert web_client.post("/api/review",
                           json={"action": "nope", "ids": [1]}).status_code == 400
    assert web_client.post("/api/review",
                           json={"action": "approve", "ids": []}).status_code == 400


def test_retry_accepts_failed_and_rejects_queued(web_client):
    d = _db(web_client)
    d.upsert_book("loc", {"source_uid": "retry-me", "collection": "test-set"})
    row = d.find_book("loc", "retry-me")
    d.set_status(row["id"], "failed")
    r = web_client.post("/api/retry", json={"id": row["id"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert d.get_book(row["id"])["status"] == "queued"
    r = web_client.post("/api/retry", json={"id": row["id"]})   # 已 queued
    assert r.status_code == 400
    assert web_client.post("/api/retry", json={"id": 999999}).status_code == 404


def test_retry_dead_resets_attempt(web_client):
    """dead 的重试必须 reset_attempt，否则永远卡在 attempt 上限外。"""
    d = _db(web_client)
    d.upsert_book("loc", {"source_uid": "dead-retry", "collection": "test-set"})
    row = d.find_book("loc", "dead-retry")
    with d.connect() as conn:
        conn.execute("UPDATE books SET status='dead', attempt=5 WHERE id=?",
                     (row["id"],))
    assert web_client.post("/api/retry", json={"id": row["id"]}).status_code == 200
    got = d.get_book(row["id"])
    assert got["attempt"] == 0 and got["status"] == "queued"


# =====================================================================
# 8. 设置页保存（批 1 第 1 项的结构前提）
# =====================================================================
def test_settings_save_persists_all_sources(web_client):
    d = _db(web_client)
    with d.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM sources ORDER BY id")]
    form = {}
    for sid in ids:
        form[f"quota_{sid}"] = "7"
        form[f"quality_{sid}"] = "pdf"
        form[f"catalog_url_{sid}"] = "https://example.invalid/catalog"
    r = web_client.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303
    with d.connect() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM sources")}
    for sid in ids:
        assert rows[sid]["hourly_quota"] == 7, (sid, rows[sid]["hourly_quota"])
        assert rows[sid]["quality"] == "pdf", sid
        assert rows[sid]["catalog_url"] == "https://example.invalid/catalog", sid


def test_upload_snapshot_rejects_unknown_source(web_client):
    r = web_client.post("/upload-snapshot",
                        data={"source": "not-a-source"},
                        files={"files": ("a.json", b"{}", "application/json")})
    assert r.status_code == 400


def test_upload_snapshot_imports(web_client):
    fixtures = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "fixtures", "loc")
    names = sorted(os.listdir(fixtures))
    files = [("files", (n, open(os.path.join(fixtures, n), "rb"),
                        "application/json")) for n in names]
    r = web_client.post("/upload-snapshot", data={"source": "loc"},
                        files=files, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    d = _db(web_client)
    assert d.count_books(status="discovered") >= 40
    row = d.find_book("loc", "2018421651")
    assert row is not None and json.loads(row["pdf_urls"] or "[]")
