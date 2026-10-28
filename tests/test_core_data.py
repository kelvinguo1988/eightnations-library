"""域测试：数据与采集适配器（DB 语义 / 导入器 / 收割器 / 下载完整性）。

这些用例对应批 2 的修复（docs/review/FIX-BATCH2.md）与审查 A/C 的缺陷，
每一条都是"把业务代码改坏就会红"的守卫测试。

覆盖点:
  * 快照导入 + era/subjects 多值筛选
  * 日文新字体→繁体
  * 日本馆分类富化
  * na_jp 收割器：预算截断 / 限流中止 / exhausted 严格四条件 / start_page 游标
  * BnF SRU 解析
  * 挂载检测
  * jobs 台账小时配额（含 59/61 分钟边界）+ 认领原子性
  * 租约回收 reap_stuck
  * 落盘路径段校验
  * 下载完整性（chunked / Content-Length / full_size 跨 attempt / validator）
  * 流式 PDF 原子写出
  * 书名硬链接幂等（必须用非空库）
  * importer 合并 item 详情
  * na_jp 多卷分块事务语义
  * 弱成功 + needs_topup 补齐
  * attempt 上限 / dead force / 人工重置
  * catalog_cursor / _norm_stats
"""
import json
import os

import pytest

import core.pipeline as pl

from conftest import FIXTURES, is_valid_pdf, make_queued, write_pdf

pytestmark = pytest.mark.guard


# =====================================================================
# 1. 快照导入 + 筛选
# =====================================================================
def test_snapshot_import_and_filters(db):
    from core.importer import import_snapshot_files
    new, upd = import_snapshot_files(db, "loc", str(FIXTURES / "loc"))
    assert new >= 40, (new, upd)
    assert db.count_books(status="discovered", era="明,清") >= 40
    assert db.count_books(status="discovered", era="unknown") >= 0
    assert db.count_books(status="discovered", subjects="不存在的分类") == 0


# =====================================================================
# 2. 繁体转换
# =====================================================================
def test_jp2t_traditional_chinese():
    from core.text import jp2t
    assert jp2t("欽定古今図書集成") == "欽定古今圖書集成"
    assert jp2t("太平御覧") == "太平御覽"
    assert jp2t("Yuan ye") == "Yuan ye"
    assert jp2t(None) is None


# =====================================================================
# 3. 日本馆分类富化
# =====================================================================
def test_na_jp_category_enrichment():
    from core.models import BookMeta
    from sites.na_jp import NaJpAdapter
    m = BookMeta(source_uid="x", title="朱子語類")
    html = ('<dt>旧蔵者</dt><dd>昌平坂学問所</dd>'
            '<dt>書誌事項</dt><dd>刊本:朝鮮:::</dd>'
            '<dt>言語</dt><dd>中国語</dd>')
    NaJpAdapter.enrich_categories(m, NaJpAdapter.parse_item_page(html))
    assert m.subjects == ["漢籍", "昌平坂学問所旧蔵", "朝鮮刊本", "中文"], m.subjects


# =====================================================================
# 4. na_jp 收割器
# =====================================================================
def _manifest_for(vid):
    return {"sequences": [{"canvases": [{"images": [{"resource": {
        "@id": f"https://x/da12/C{vid}001"}}]}]}],
        "label": f"件名{vid}"}


class HarvestHttp:
    """按页给出不同 id 的假站点；pages 为 [[ids], [ids], ...]，空 list = 到底。"""

    def __init__(self, pages, fail_vids=(), page_fail_from=None):
        self.pages = pages
        self.fail_vids = set(map(str, fail_vids))
        self.page_fail_from = page_fail_from     # 从第 N 次列表请求起返回 None
        self.page_calls = 0
        self.mf_calls = 0
        self.seen_pages = []

    def get(self, url, as_json=False, **kw):     # noqa: ARG002
        if "/fonds/" in url or "/search" in url:
            self.page_calls += 1
            self.seen_pages.append(url)
            if self.page_fail_from and self.page_calls >= self.page_fail_from:
                return None
            # 站点行为：超过最后一页 → 空页面（这才是"翻到底"的信号）。
            # 注意空页**也**是一次真实请求，pages 列表里不需要显式写 []。
            idx = self.page_calls - 1
            ids = self.pages[idx] if idx < len(self.pages) else []
            return "".join(f'<a href="/img/{i}">' for i in ids)
        self.mf_calls += 1
        vid = url.rstrip("/").split("/")[-2]
        if vid in self.fail_vids:
            return None
        mf = _manifest_for(vid)
        return mf if as_json else json.dumps(mf)


NA_JP_KEYS = {"ids", "fetched", "skipped", "blocked", "exhausted",
              "reached_end", "next_page", "failed"}


def test_harvest_contract_keys_and_backward_compat():
    """契约：8 个键齐全；旧 5 键语义不变（批 2 的纯增量扩展）。"""
    from sites.na_jp import NaJpAdapter
    # catalog_url 必须带 ?page=1：不带 query 时实现会把单页 HTML 当成"无页码
    # 目录"整体返回，再扫第 2 页就会把同一页内容当新页（见 FIX-BATCH3 §5）
    a = NaJpAdapter(HarvestHttp([["101", "102", "103", "104", "105"]]))
    s = a.harvest_step("https://x/fonds/99?page=1", budget=2, max_pages=3)
    assert set(s) == NA_JP_KEYS, s
    # 旧 5 键子集（tests/test_regressions.py:82 的断言放宽为子集）
    assert {k: s[k] for k in ("ids", "fetched", "skipped", "blocked",
                              "exhausted")} == {
        "ids": 5, "fetched": 2, "skipped": 0, "blocked": False,
        "exhausted": False}, s
    # 新键语义：budget 截断 → 不封存；有失败 → failed 计数；是否到底如实上报
    assert s["exhausted"] is False
    assert s["failed"] == 0


def test_harvest_blocked_after_three_manifest_failures():
    from sites.na_jp import NaJpAdapter
    seen = []
    a = NaJpAdapter(HarvestHttp([["201", "202", "203", "204", "205"]],
                                fail_vids={"203", "204", "205"}))
    s = a.harvest_step("https://x/fonds/99", budget=0,
                       on_meta=lambda m: seen.append(m.source_uid))
    assert s["blocked"] and seen == ["201", "202"], (s, seen)
    assert s["exhausted"] is False       # 被限流绝不算"处理完"


# --- exhausted 严格四条件（FIX-BATCH2 §B2，审查 C P0-4） -----------------
def test_exhausted_all_known_single_page():
    """① 页面只有一页且全部已在库 → 真的到底 → exhausted=True。"""
    from sites.na_jp import NaJpAdapter
    h = HarvestHttp([["301", "302"]])
    s = NaJpAdapter(h).harvest_step("https://x/fonds/99",
                                    known_uids={"301", "302"}, budget=10)
    assert s["exhausted"] is True, s
    assert s["reached_end"] is True and s["fetched"] == 0 and s["skipped"] == 2


def test_exhausted_false_when_budget_truncated():
    """② budget 截断（还有下一页/还有条目）→ 绝不 exhausted。

    回归点：修前实现是 `fetched < budget`，两页目录 + budget=2 时会误判完成，
    把 5268 件的目录封存 7 天。
    """
    from sites.na_jp import NaJpAdapter
    pages = [[str(400 + i) for i in range(100)],
             [str(500 + i) for i in range(100)]]
    h = HarvestHttp(pages)
    s = NaJpAdapter(h).harvest_step("https://x/fonds/99", budget=2, max_pages=2)
    assert s["exhausted"] is False, s
    assert s["reached_end"] is False, s
    assert s["fetched"] == 2
    assert s["next_page"] >= 2, s


def test_exhausted_true_only_when_reached_end():
    """③ 两页目录、预算充足、第三页为空 → 到底 → exhausted=True。"""
    from sites.na_jp import NaJpAdapter
    pages = [["601", "602"], ["603", "604"]]     # 第 3 次请求 → 空页
    h = HarvestHttp(pages)
    s = NaJpAdapter(h).harvest_step("https://x/fonds/99", budget=50, max_pages=5)
    assert s["exhausted"] is True, s
    assert s["reached_end"] is True and s["fetched"] == 4
    assert h.page_calls == 3


def test_exhausted_false_when_max_pages_exhausted():
    """④ 翻页上限用尽但目录还有下一页 → 不算到底 → 不可封存。"""
    from sites.na_jp import NaJpAdapter
    pages = [["701", "702"], ["703", "704"], ["705", "706"]]
    h = HarvestHttp(pages)
    s = NaJpAdapter(h).harvest_step("https://x/fonds/99", budget=50, max_pages=2)
    assert s["exhausted"] is False, s
    assert s["fetched"] == 4, s          # 只扫了 2 页
    assert h.page_calls == 2, h.page_calls


def test_exhausted_false_when_details_failed():
    """⑤ 有详情获取失败（遗漏）→ 不可封存，failed>0 如实上报。"""
    from sites.na_jp import NaJpAdapter
    h = HarvestHttp([["801", "802", "803"]], fail_vids={"801", "802"})
    s = NaJpAdapter(h).harvest_step("https://x/fonds/99", budget=0, max_pages=2)
    assert s["failed"] == 2, s
    assert s["exhausted"] is False


def test_harvest_start_page_cursor():
    """start_page 游标：从第 3 页起扫，只请求该页。"""
    from sites.na_jp import NaJpAdapter
    h = HarvestHttp([["901"], ["902"], ["903"]])
    s = NaJpAdapter(h).harvest_step("https://x/fonds/99?page=1", budget=0,
                                    max_pages=2, start_page=3)
    assert h.seen_pages and h.seen_pages[0].endswith("page=3"), h.seen_pages
    assert h.page_calls == 2, h.page_calls
    assert s["fetched"] >= 1, s


# =====================================================================
# 5. BnF SRU 解析
# =====================================================================
def test_bnf_sru_parse_fixture():
    from sites.bnf import _parse_sru
    fx = FIXTURES / "bnf_sru_sample.xml"
    assert fx.exists(), "fixture 缺失（此前的测试会在缺失时静默跳过）"
    r = _parse_sru(fx.read_text(encoding="utf-8"))
    assert r["total"] >= 1000
    assert r["records"][0]["ark"].startswith("btv1")


# =====================================================================
# 6. 挂载检测
# =====================================================================
def test_mounts_and_migration_hint():
    from core.mounts import data_mount, migration_commands
    assert data_mount()["kind"] in ("bind", "volume", "unknown")
    assert "migrate-nas.sh" in migration_commands("x")


# =====================================================================
# 7. jobs 台账配额 + 认领原子性
# =====================================================================
def test_quota_ledger_atomic_claim(db):
    b1 = make_queued(db, "quota-uid-1", collection="quota-set")["id"]
    b2 = make_queued(db, "quota-uid-2", collection="quota-set")["id"]
    job1, full1, claimed1 = db.claim_and_start(b1, "auto", quota_limit=1)
    assert claimed1 and not full1 and job1
    job2, full2, claimed2 = db.claim_and_start(b2, "auto", quota_limit=1)
    assert not claimed2 and full2        # 本小时已启动 1 册 = 配额用尽
    assert db.get_book(b2)["status"] == "queued"   # 满额不扣、不改状态
    _, _, again = db.claim_and_start(b1, "auto")
    assert not again                     # running：不可重复认领
    _, _, c3 = db.claim_and_start(b2, "auto")      # 不限额仍可认领
    assert c3
    db.job_heartbeat(job1)


@pytest.mark.parametrize("minutes_ago,expect_blocked", [
    (59, True),      # 59 分钟前启动：仍在 1 小时窗口内 → 占配额
    (61, False),     # 61 分钟前启动：窗口外 → 不占配额
])
def test_quota_window_boundary(db, minutes_ago, expect_blocked):
    """配额窗口是 1 小时（不是 1 分钟）：59 分钟前计入、61 分钟前不计入。

    变异"hours=1 → minutes=1"：61 分钟前的 job 会被算进本小时（应不计），
    而 59 分钟的用例在 minutes=1 下也会变成"不计入"，两条同时断言才抓得住。
    """
    from datetime import datetime, timedelta, timezone
    old = make_queued(db, f"old-job-{minutes_ago}", collection="win-set")["id"]
    job, _, claimed = db.claim_and_start(old, "auto")
    assert claimed
    started = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET started_at=? WHERE id=?", (started, job))
    new = make_queued(db, f"new-book-{minutes_ago}", collection="win-set")["id"]
    _, full, claimed2 = db.claim_and_start(new, "auto", quota_limit=1)
    assert full is expect_blocked, (
        f"{minutes_ago} 分钟前启动的 job 应{'占' if expect_blocked else '不占'}配额，"
        f"full={full}")
    assert claimed2 is (not expect_blocked)


# =====================================================================
# 8. 租约回收
# =====================================================================
def test_reap_stuck_lease(db):
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid

    def _add_job(book_id, pid, started, heart=""):
        with db._immediate() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(book_id,state,quality,started_at,pid,heartbeat_at) "
                "VALUES(?,'running','auto',?,?,?)", (book_id, started, pid, heart))
            return int(cur.lastrowid)

    b3 = make_queued(db, "reap-dead-pid")["id"]
    db.claim_and_start(b3, "auto")
    with db._immediate() as conn:
        conn.execute("UPDATE jobs SET pid=? WHERE book_id=?", (dead_pid, b3))
    b4 = make_queued(db, "reap-stale")["id"]
    sj = _add_job(b4, os.getpid(), "2020-01-01T00:00:00Z")
    with db._immediate() as conn:
        conn.execute("UPDATE books SET status='running' WHERE id=?", (b4,))
    b5 = make_queued(db, "reap-healthy")["id"]
    with db._immediate() as conn:
        conn.execute("UPDATE books SET status='running' WHERE id=?", (b5,))
    hj = _add_job(b5, os.getpid(), "2099-01-01T00:00:00Z")

    n = db.reap_stuck(stale_minutes=60)
    assert n >= 2, n
    assert db.get_book(b3)["status"] == "queued"
    assert db.get_book(b4)["status"] == "queued"
    assert db.get_book(b5)["status"] == "running", "在途下载不得被回收"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id=?",
                            (sj,)).fetchone()["state"] == "failed"
        assert conn.execute("SELECT state FROM jobs WHERE id=?",
                            (hj,)).fetchone()["state"] == "running"


# =====================================================================
# 9. 路径段校验
# =====================================================================
def test_dest_dir_and_upsert_reject_traversal(db):
    from core.pipeline import _dest_dir
    for bad_uid in ("", ".", "..", "../etc", "a/b", "a\\b", "a\x00b"):
        with pytest.raises(ValueError):
            _dest_dir("loc", "coll", bad_uid)
    with pytest.raises(ValueError):
        _dest_dir("loc", "../../etc", "u")
    assert _dest_dir("loc", "", "u1").endswith(os.path.join("misc", "u1"))
    for meta_bad in ({"source_uid": "../x", "collection": "c"},
                     {"source_uid": "", "collection": "c"},
                     {"source_uid": "u", "collection": "a/b"}):
        with pytest.raises(ValueError):
            db.upsert_book("loc", meta_bad)
    assert db.upsert_book("loc", {"source_uid": "empty-coll-ok",
                                  "collection": ""})


# =====================================================================
# 10. 下载完整性
# =====================================================================
def test_download_chunked_interrupt_rejected(http_factory, tmp_path):
    import requests
    from conftest import FakeResp
    hc, _ = http_factory(
        FakeResp(200, [b"a" * 2000], exc=requests.exceptions.ChunkedEncodingError()))
    dest = str(tmp_path / "cut.jpg")
    assert not hc.download("https://x/y.jpg", dest, min_bytes=1000)
    assert not os.path.exists(dest)


def test_download_complete_chunked_accepted(http_factory, tmp_path):
    from conftest import FakeResp
    hc, _ = http_factory(FakeResp(200, [b"b" * 6000, b"c" * 5000]))
    dest = str(tmp_path / "full.jpg")
    assert hc.download("https://x/y.jpg", dest, min_bytes=1000)
    assert os.path.getsize(dest) == 11000


def test_download_content_length_mismatch_rejected(http_factory, tmp_path):
    from conftest import FakeResp
    hc, _ = http_factory(FakeResp(200, [b"d" * 1000],
                                  headers={"Content-Length": "9000"}))
    dest = str(tmp_path / "short.jpg")
    assert not hc.download("https://x/y.jpg", dest, min_bytes=1000)
    assert not os.path.exists(dest)


def test_download_full_size_not_leaked_across_attempts(http_factory, tmp_path):
    """A1（审查 A P0-2）：上一轮 206 的总长不得用于判定下一轮。

    场景：先有 5000B 残片；第 1 次响应是 206 且拿不到 Content-Range 总长
    （修前会把 5000 记成 full_size 并继续追加）；第 2 次是全量 4000B（200）。
    修前：5000+4000=9000 == 泄漏的 full_size → 判成功（错位文件）。
    修后：每轮重置 attempt_size，最终按 4000B 判定。
    """
    from conftest import FakeResp
    dest = tmp_path / "f.pdf"
    (tmp_path / "f.pdf.part").write_bytes(b"a" * 5000)
    first = FakeResp(206, [b"a" * 1], headers={})        # 无 Content-Range
    second = FakeResp(200, [b"c" * 4000],
                      headers={"Content-Length": "4000"})
    hc, stub = http_factory(first, second, attempts=2)
    ok = hc.download("https://x/f.pdf", str(dest), min_bytes=100)
    assert ok, "第二次是全量 200，应成功"
    assert dest.read_bytes() == b"c" * 4000, "必须是全量重下的内容，不能是拼接残片"
    assert not (tmp_path / "f.pdf.part").exists()


def test_download_skip_if_exists_without_validator(http_factory, tmp_path):
    """向后兼容：不传 validator 时体积达标即跳过（旧行为，页图/封面依赖它）。"""
    dest = tmp_path / "a.jpg"
    dest.write_bytes(b"x" * 2000)
    hc, stub = http_factory()
    assert hc.download("https://x/a.jpg", str(dest), min_bytes=1000)
    assert stub.calls == [], "已存在且达标时不应发请求"


def test_download_validator_rejects_bad_pdf(http_factory, tmp_path):
    """A2（审查 A P0-1）：skip-if-exists 必须过 validator，坏 PDF 要重下。"""
    from conftest import FakeResp
    dest = tmp_path / "b.pdf"
    dest.write_bytes(b"%PDF-1.4" + b"\x00" * 5000)     # 头像 PDF，内容不可读
    good = str(tmp_path / "good.pdf")
    write_pdf(good, pages=2)
    good_bytes = open(good, "rb").read()
    hc, stub = http_factory(FakeResp(
        200, [good_bytes], headers={"Content-Length": str(len(good_bytes))}))
    ok = hc.download("https://x/b.pdf", str(dest), min_bytes=100,
                     validator=is_valid_pdf)
    assert ok
    assert is_valid_pdf(dest), "坏文件必须被替换成有效 PDF"
    assert len(stub.calls) == 1, "坏文件必须触发一次真实下载"


def test_download_validator_rejects_final_bad_payload(http_factory, tmp_path):
    """下载完成后仍不可读 → 判失败且不留坏文件。"""
    from conftest import FakeResp
    bad = b"%PDF-1.4" + b"\x00" * 4000
    hc, _ = http_factory(
        FakeResp(200, [bad], headers={"Content-Length": str(len(bad))}),
        FakeResp(200, [bad], headers={"Content-Length": str(len(bad))}),
        attempts=2)
    dest = tmp_path / "c.pdf"
    assert not hc.download("https://x/c.pdf", str(dest), min_bytes=100,
                           validator=is_valid_pdf)
    assert not dest.exists()
    assert not (tmp_path / "c.pdf.part").exists()


# =====================================================================
# 11. 流式 PDF 原子性
# =====================================================================
def test_build_pdf_atomic_on_bad_jpeg(jpeg_factory, tmp_path):
    from core import pdfbuild
    good = jpeg_factory("good.jpg")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xfe not-a-complete-jpeg-data")
    half = tmp_path / "half.pdf"
    with pytest.raises(ValueError):
        pdfbuild.build_pdf([good, str(bad)], str(half))
    assert not half.exists() and not (tmp_path / "half.pdf.part").exists()


def test_build_pdf_page_count(jpeg_factory, tmp_path):
    from core import pdfbuild
    from pypdf import PdfReader
    paths = [jpeg_factory(f"p{i}.jpg", size=(700 + i, 1000)) for i in range(6)]
    out = tmp_path / "t.pdf"
    pdfbuild.build_pdf(paths, str(out))
    assert len(PdfReader(str(out)).pages) == 6
    g = tmp_path / "g.pdf"
    pdfbuild.build_pdf(paths[:2] + paths[4:], str(g))
    assert len(PdfReader(str(g)).pages) == 4


# =====================================================================
# 12. 书名硬链接幂等（必须非空库）
# =====================================================================
def test_title_links_idempotent_on_nonempty_library(db, data_dir):
    """变异 #3：`_title_link` 恒返回 "new"。

    旧测试用空库，断言退化成 0==0 才让变异逃逸。这里先真正造出一本
    done 且有 PDF 的书（含多卷，覆盖 _NN 后缀分支），再断言首跑 >0、二跑 ==0。
    """
    from core.pipeline import _dest_dir, _sanitize_filename, create_title_links
    assert _sanitize_filename('永樂大典/卷六十八:十八陽') == "永樂大典_卷六十八_十八陽"
    row = make_queued(db, "hardlink-uid", collection="link-set",
                      title="永樂大典", alt_title="永樂大典卷六十八")
    dest = _dest_dir("loc", "link-set", "hardlink-uid")
    os.makedirs(dest, exist_ok=True)
    write_pdf(os.path.join(dest, "book_01.pdf"), 1)
    write_pdf(os.path.join(dest, "book_02.pdf"), 1)
    db.record_done(row["id"], db.start_job(row["id"], "auto"), 2, 2, 1234,
                   [os.path.join(dest, "book_01.pdf"),
                    os.path.join(dest, "book_02.pdf")])
    # 非空前置条件：库里有 1 本 done、磁盘上有 2 个实体 PDF
    assert db.count_books(status="done") == 1
    assert len([n for n in os.listdir(dest) if n.startswith("book_")]) == 2

    c1, s1 = create_title_links(db)
    assert c1 == 2, f"首跑应建 2 条书名链接（实际 {c1}，skipped={s1}）"
    c2, s2 = create_title_links(db)
    assert c2 == 0, f"二跑必须全部'已存在'（实际 new={c2}）"
    assert s2 == 2, s2
    links = [n for n in os.listdir(dest) if n.startswith("hardlink-uid_")]
    assert len(links) == 2, links


# =====================================================================
# 13. importer 合并 item 详情（变异 #1）
# =====================================================================
def test_importer_merges_item_details(db):
    """集合级快照只给 `files` 整数页数；item_<uid>.json 才给 pdf 直链与逐页清单。

    变异"删掉 importer.py 里合并详情那一行"：pdf_urls 与 files_json 全丢，
    这本书就再也下载不出东西——旧测试对此毫无断言。
    """
    from core.importer import import_snapshot_files
    new, _ = import_snapshot_files(db, "loc", str(FIXTURES / "loc"))
    assert new >= 40
    row = db.find_book("loc", "2018421651")
    assert row is not None, "fixture 里的条目应已入库"
    pdfs = json.loads(row["pdf_urls"] or "[]")
    pages = json.loads(row["files_json"] or "[]")
    assert any(pdfs), f"官方 PDF 直链没有被合并进库: {pdfs!r}"
    assert pdfs[0].endswith("/2018421651.pdf"), pdfs[0]
    assert pages and len(pages[0]) >= 90, f"逐页清单没有被合并: {len(pages)}"
    assert row["page_count"] >= 90


# =====================================================================
# 14. na_jp 多卷分块事务语义（审查 C P0-1）
# =====================================================================
class _FakePdf:
    """把'页数'编码进文件头的假 pypdf（只实现用到的两个类）。"""

    class PdfReader:
        def __init__(self, path):
            with open(path, "rb") as f:
                head = f.read(16)
            if not head.startswith(b"%PDF"):
                raise ValueError("bad pdf")
            self.pages = [0] * int(head[4:10].strip() or 0)

    class PdfWriter:
        def __init__(self):
            self.pages = []

        def add_page(self, p):
            self.pages.append(p)

        def write(self, f):
            f.write(b"%PDF" + str(len(self.pages)).encode().ljust(6) + b"\n")


@pytest.fixture()
def najp(monkeypatch):
    """装载 na_jp 并把 pypdf/time.sleep 换成无损测试桩。"""
    import sites.na_jp as mod
    fake = type("pypdf", (), {"PdfReader": _FakePdf.PdfReader,
                              "PdfWriter": _FakePdf.PdfWriter})
    monkeypatch.setitem(__import__("sys").modules, "pypdf", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None, raising=False)
    return mod


def _pdf_bytes(pages):
    return b"%PDF" + str(pages).encode().ljust(6) + b"\n"


class ChunkHttp:
    """contentDownload 桩：按 (vid, 缺块序号) 决定哪些块失败。"""

    class _Throttle:
        def wait(self, url):
            return None

    def __init__(self, fail=None):
        self.fail = set(fail or ())
        self.calls = []
        self.throttle = ChunkHttp._Throttle()
        self.session = self

    def post(self, url, data=None, timeout=None):     # noqa: ARG002
        vid = url.split("/")[-1].split("?")[0]
        n = len(data)
        self.calls.append((vid, n))
        if (vid, n) in self.fail:
            return type("R", (), {"status_code": 200,
                                  "content": b"<html>challenge</html>"})()
        return type("R", (), {"status_code": 200, "content": _pdf_bytes(n)})()


def test_na_jp_partial_failure_reports_completed_volumes(najp, tmp_path):
    """B1（审查 C P0-1）：卷 2 失败时，已完成的卷 1 必须照实上报（不是 return 0）。"""
    vols = [("v1", ["a", "b"], {}), ("v2", ["c", "d"], {})]
    http = ChunkHttp(fail={("v2", 2)})
    errors = []
    total, outputs = najp.NaJpAdapter(http)._download_official(
        vols, str(tmp_path), http, errors, None)
    names = sorted(os.path.basename(p) for p in outputs)
    assert total == 2, (total, outputs, errors)
    assert names == ["book_01.pdf"], names
    on_disk = sorted(n for n in os.listdir(tmp_path) if n.endswith(".pdf"))
    assert on_disk == names, f"上报与磁盘必须一致: 上报={names} 磁盘={on_disk}"
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".part")], \
        "不得留下无主 .part 文件"
    assert any("卷2" in e for e in errors), errors


def test_na_jp_all_volumes_ok_no_page_cross_contamination(najp, tmp_path):
    """全成功：每卷页数 == 该卷 cid 数（不得把上一卷的块混进来）。"""
    vols = [("v1", ["a"], {}), ("v2", ["b", "c"], {})]
    http = ChunkHttp()
    total, outputs = najp.NaJpAdapter(http)._download_official(
        vols, str(tmp_path), http, [], None)
    assert total == 3
    got = {os.path.basename(p): len(_FakePdf.PdfReader(p).pages) for p in outputs}
    assert got == {"book_01.pdf": 1, "book_02.pdf": 2}, got


def test_na_jp_retry_keeps_completed_volume(najp, tmp_path):
    """重试时已完成的卷不被重下、也不被卷 2 污染（孤儿被洗白的回归点）。"""
    vols = [("v1", ["a"], {}), ("v2", ["b"], {})]
    http = ChunkHttp(fail={("v2", 1)})
    najp.NaJpAdapter(http)._download_official(
        vols, str(tmp_path), http, [], None)
    v1 = tmp_path / "book_01.pdf"
    assert v1.exists()
    mtime = v1.stat().st_mtime_ns
    http2 = ChunkHttp()
    total, outputs = najp.NaJpAdapter(http2)._download_official(
        vols, str(tmp_path), http2, [], None)
    assert total == 2
    assert v1.stat().st_mtime_ns == mtime, "卷 1 不应被重下"
    assert ("v1", 1) not in http2.calls, http2.calls
    assert {os.path.basename(p) for p in outputs} == {"book_01.pdf",
                                                      "book_02.pdf"}


def test_na_jp_volume_names_all_reader_visible():
    """B3：多卷一律 book_NN.pdf（旧实现会产出下游不认的 cover.pdf）。"""
    import re
    from sites.na_jp import NaJpAdapter
    visible = re.compile(r"^(book\.pdf|book_\d+\.pdf)$")
    for n in (1, 2, 3, 5):
        vols = [(f"v{i}", ["c"], {}) for i in range(n)]
        names = NaJpAdapter._volume_names(vols)
        assert all(visible.match(x) for x in names), (n, names)
        assert len(set(names)) == n


# =====================================================================
# 15. 弱成功 → needs_topup 补齐（C1）
# =====================================================================
class _AdapterStub:
    """pipeline 测试用适配器：返回预设的 DownloadResult。"""

    def __init__(self, result):
        self.result = result
        self.http = None
        self.calls = 0

    def download_item(self, meta, dest, http, quality, progress):   # noqa: ARG002
        self.calls += 1
        os.makedirs(dest, exist_ok=True)
        for p in self.result.outputs:
            if not os.path.exists(p):
                write_pdf(p, 1)
        return self.result


def _patch_adapter(monkeypatch, stub):
    monkeypatch.setattr(pl, "get_adapter", lambda sid, http: stub)


def test_partial_success_stays_in_queue_until_topped_up(db, data_dir, monkeypatch):
    """C1：多卷只成功 1 卷 → done + needs_topup=1，且仍在队列里；补齐后归 0。"""
    from core.models import DownloadResult
    from core.pipeline import _dest_dir, fetch_one

    row = make_queued(db, "topup-uid", collection="topup-set",
                      page_count=300, volume_count=2)
    dest = _dest_dir("loc", "topup-set", "topup-uid")
    first = DownloadResult(ok=True, outputs=[os.path.join(dest, "book_01.pdf")],
                           pages=120, bytes_done=1000, errors=["卷2 失败"],
                           missing=["卷2"])
    stub = _AdapterStub(first)
    _patch_adapter(monkeypatch, stub)
    assert fetch_one(db, row, "auto") is True

    got = db.get_book(row["id"])
    assert got["status"] == "done", "部分完成仍是 done（不新增状态）"
    assert got["needs_topup"] == 1
    assert "卷2" in (got["last_error"] or "")
    assert got["page_count"] == 300, "弱成功不得把 catalog 页数改小（C4）"
    queue = [r["id"] for r in db.queued_books("loc", limit=10)]
    assert row["id"] in queue, f"部分完成的书必须留在补位队列里: {queue}"

    second = DownloadResult(ok=True,
                            outputs=[os.path.join(dest, "book_01.pdf"),
                                     os.path.join(dest, "book_02.pdf")],
                            pages=300, bytes_done=3000)
    stub2 = _AdapterStub(second)
    _patch_adapter(monkeypatch, stub2)
    cur = db.get_book(row["id"])
    assert fetch_one(db, cur, "auto") is True
    got2 = db.get_book(row["id"])
    assert got2["needs_topup"] == 0, "补齐成功后 needs_topup 必须归 0"
    assert row["id"] not in [r["id"] for r in db.queued_books("loc", limit=10)]
    assert got2["attempt"] == got["attempt"], "补齐不是重试，attempt 不应递增"


def test_ok_with_zero_pages_is_failure(db, data_dir, monkeypatch):
    """弱成功：ok=True 但 0 页 → 视为失败（不得记 done）。"""
    from core.models import DownloadResult
    from core.pipeline import fetch_one
    row = make_queued(db, "zeropage-uid")
    stub = _AdapterStub(DownloadResult(ok=True, outputs=[], pages=0))
    _patch_adapter(monkeypatch, stub)
    assert fetch_one(db, row, "auto") is True
    got = db.get_book(row["id"])
    assert got["status"] == "failed", got["status"]


# =====================================================================
# 16. attempt 上限 / dead 需 force
# =====================================================================
def test_attempt_limit_stops_claiming_and_retry_resets(db, data_dir, monkeypatch):
    """C2：连续失败到上限后不再被认领（不消耗配额）；人工重试可重跑。"""
    from core.db import MAX_ATTEMPTS
    from core.models import DownloadResult
    from core.pipeline import fetch_one
    row = make_queued(db, "attempt-uid")
    stub = _AdapterStub(DownloadResult(ok=False, errors=["boom"]))
    _patch_adapter(monkeypatch, stub)
    runs = 0
    while runs < MAX_ATTEMPTS + 3:
        cur = db.get_book(row["id"])
        if cur["status"] not in ("queued", "failed"):
            break
        fetch_one(db, cur, "auto", quota_n=100)
        runs += 1
    got = db.get_book(row["id"])
    assert runs == MAX_ATTEMPTS, f"应恰好认领 {MAX_ATTEMPTS} 次，实际 {runs}"
    assert got["status"] == "dead", got["status"]
    with db.connect() as conn:
        jobs = conn.execute("SELECT COUNT(*) n FROM jobs WHERE book_id=?",
                            (row["id"],)).fetchone()["n"]
    assert jobs == MAX_ATTEMPTS, f"到上限后不得继续建 job（配额泄漏）: {jobs}"
    # dead 无 force 不可认领
    _, _, claimed = db.claim_and_start(row["id"], "auto")
    assert not claimed
    # 人工重试：attempt 归零后可重新认领
    db.set_status(row["id"], "queued", reset_attempt=True)
    assert db.get_book(row["id"])["attempt"] == 0
    job, _, ok = db.claim_and_start(row["id"], "auto")
    assert ok and job


def test_dead_requires_force(db):
    row = make_queued(db, "dead-uid")
    job, _, ok = db.claim_and_start(row["id"], "auto")
    assert ok
    db.set_status(row["id"], "dead")
    _, _, no_force = db.claim_and_start(row["id"], "auto")
    assert no_force is False, "dead 无 force 不得被认领"
    job2, _, forced = db.claim_and_start(row["id"], "auto", force=True)
    assert forced and job2


def test_max_attempts_constant_exposed_from_db():
    import core.db as dbmod
    assert dbmod.MAX_ATTEMPTS == 5
    assert pl.MAX_ATTEMPTS is dbmod.MAX_ATTEMPTS, \
        "MAX_ATTEMPTS 必须只有一份（pipeline 从 db 导入）"


# =====================================================================
# 17. catalog_cursor / _norm_stats（scheduler）
# =====================================================================
def test_catalog_cursor_roundtrip_and_reset(db):
    assert db.get_catalog_cursor("na_jp") == 1
    db.set_catalog_cursor("na_jp", 3)
    assert db.get_catalog_cursor("na_jp") == 3
    db.set_catalog_time("na_jp")            # 封存：游标回落 1
    assert db.get_catalog_cursor("na_jp") == 1


def test_norm_stats_never_raises_and_never_returns_none_fields():
    import scheduler as sch
    for bad in (None, {}, {"ids": None}, {"blocked": "yes"},
                {"exhausted": None}, {"next_page": 0}, [1, 2, 3]):
        out = sch._norm_stats(bad)
        assert set(out) >= {"ids", "fetched", "skipped", "blocked",
                            "exhausted", "reached_end", "next_page", "failed"}
        for k, v in out.items():
            assert v is not None, (bad, k)
        assert out["exhausted"] is False, f"不确定的 stats 绝不能封存: {bad}"
    ok = sch._norm_stats({"ids": 3, "fetched": 1, "skipped": 2,
                          "blocked": False, "exhausted": True,
                          "reached_end": True, "next_page": 1, "failed": 0})
    assert ok["exhausted"] is True


# =====================================================================
# 18. 变异测试暴露的覆盖缺口（tests/mutation_check.py 曾判为 SURVIVED）
# =====================================================================
def test_download_full_size_leak_with_known_expected_bytes(http_factory, tmp_path):
    """A1 加强版：**给了 expected_bytes 时**，上一轮的总长也不得复用到下一轮。

    前一个用例（test_download_full_size_not_leaked_across_attempts）没传
    expected_bytes，所以把 `attempt_size = expected_bytes` 挪到循环外这种变异
    是**语义等价**的（None 挪到哪都是 None）→ 变异逃逸、测试假绿。
    这里显式传 expected_bytes=9000，构造审查 A P0-2 的原始错位场景：

      残片 5000B；第 1 次 206 且无 Content-Range（修前会沿用 9000）；
      第 2 次 200 全量 4000B → 追加成 9000B，恰好等于 expected_bytes。
      修前：判成功，得到"长度对、内容错"的文件。
      修后：每轮重置，第 1 轮直接 drop 残片重下，最终只有 4000B 全量内容。
    """
    from conftest import FakeResp
    dest = tmp_path / "leak.pdf"
    (tmp_path / "leak.pdf.part").write_bytes(b"a" * 5000)
    first = FakeResp(206, [b"a" * 1], headers={})              # 无 Content-Range
    second = FakeResp(200, [b"c" * 4000],
                      headers={"Content-Length": "4000"})
    hc, stub = http_factory(first, second, attempts=2)
    ok = hc.download("https://x/leak.pdf", str(dest),
                     min_bytes=100, expected_bytes=9000)
    assert ok, "第二次是全量 200，应成功"
    assert dest.read_bytes() == b"c" * 4000, (
        "不得把两轮字节拼成 9000B 的错位文件（审查 A P0-2 原始 bug）")
    assert len(stub.calls) == 2


def test_download_416_without_expected_bytes_is_failure(http_factory, tmp_path):
    """A3 加强版：416 且调用方没给 expected_bytes 时必须判失败。

    416 只说"Range 越界"，不告诉总长。修前任何 >= min_bytes 的残片都会被
    当成完整文件接受；修后必须 drop 残片并最终返回 False。
    """
    from conftest import FakeResp
    dest = tmp_path / "r.pdf"
    (tmp_path / "r.pdf.part").write_bytes(b"a" * 5000)
    hc, stub = http_factory(FakeResp(416, [], headers={}),
                            FakeResp(416, [], headers={}), attempts=2)
    ok = hc.download("https://x/r.pdf", str(dest), min_bytes=100)
    assert ok is False, "416 无总长不得把残片当完整文件"
    assert not dest.exists(), "不得产出目标文件"


def test_attempt_cap_enforced_in_sql_not_only_in_pipeline(db):
    """C2 加强版：**直接**断言 claim_and_start 的 SQL 上限（不经过 pipeline）。

    端到端用例（test_attempt_limit_stops_claiming_and_retry_resets）里，
    pipeline 会把状态置 dead，于是"SQL 里的 attempt < MAX"即使被删掉，
    也还有 `status not in allowed` 这一层挡着 → 变异逃逸。
    这里绕过 pipeline，手工构造 status='failed' + attempt=MAX 的行，
    只有 SQL 上限能拦住它。
    """
    from core.db import MAX_ATTEMPTS
    row = make_queued(db, "sql-cap-uid")
    with db._immediate() as conn:
        conn.execute("UPDATE books SET status='failed', attempt=? WHERE id=?",
                     (MAX_ATTEMPTS, row["id"]))
    _, _, claimed = db.claim_and_start(row["id"], "auto")
    assert claimed is False, (
        f"attempt={MAX_ATTEMPTS} 的 failed 书不得再被认领（SQL 上限失效）")
    # 边界：MAX-1 必须还能认领，否则上限写错成 > 会永远拦
    with db._immediate() as conn:
        conn.execute("UPDATE books SET attempt=? WHERE id=?",
                     (MAX_ATTEMPTS - 1, row["id"]))
    job, _, claimed = db.claim_and_start(row["id"], "auto")
    assert claimed is True and job
    assert db.get_book(row["id"])["attempt"] == MAX_ATTEMPTS
