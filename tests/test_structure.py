"""域测试：模板 / 前端源码的结构断言（grep 级，零依赖）。

对应 B-web.md §9 建议的零依赖测试与批 1 的第 1/3 项修复。这些不是"看代码
好不好看"，而是**跨文件一致性契约**：模板与 JS 是手工维护的，历史上出现过
"JS 循环了 sepia 但后端白名单没有"、"写变量但不写 dataset"这类同步遗漏。

覆盖:
  * settings.html：form 零嵌套；每个 source 的输入都归属 /settings 表单
  * reader.js：必须写 root.dataset.layout
  * 模板中不得出现 `|safe`（XSS 回归守卫）
  * data-anns 注入测试：含 <img onerror> 的标注渲染后必须已转义
"""
import html as html_mod
import re
from html.parser import HTMLParser

import pytest

from conftest import ROOT

pytestmark = [pytest.mark.structural, pytest.mark.guard]

TEMPLATES = ROOT / "web" / "templates"
STATIC = ROOT / "web" / "static"


def _read(p):
    return (ROOT / p).read_text(encoding="utf-8")


# =====================================================================
# 1. settings.html：form 零嵌套 + 控件归属（批 1 第 1 项）
# =====================================================================
class _FormTree(HTMLParser):
    """解析出每个 form 的内部控件与嵌套的 form 数量。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.max_depth = 0
        self.forms = []          # [{"action":..., "controls":[...], "nested":n}]
        self._stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            if self._stack:
                self._stack[-1]["nested"] += 1
            self._stack.append({"action": a.get("action", ""),
                                "controls": [], "nested": 0})
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            return
        if self._stack and tag in ("input", "select", "textarea", "button"):
            name = a.get("name") or a.get("type")
            if name and a.get("type") != "submit":
                self._stack[-1]["controls"].append(name)

    def handle_endtag(self, tag):
        if tag == "form" and self._stack:
            self.forms.append(self._stack.pop())
            self.depth -= 1


def _parse_forms(markup):
    p = _FormTree()
    p.feed(markup)
    p.close()
    return p


def test_settings_page_has_no_nested_forms(web_client):
    """历史上内层 </form> 提前关闭外层表单，导致「保存设置」丢字段。

    断言对象是**渲染后的 HTML**（不是模板源），与浏览器看到的一致。
    """
    markup = web_client.get("/settings").text
    p = _parse_forms(markup)
    assert p.max_depth == 1, f"出现 form 嵌套（最大深度 {p.max_depth}）"
    assert p.depth == 0, "form 标签未闭合"
    assert len(p.forms) >= 2, f"应有 /settings + /upload-snapshot 两个表单: {p.forms}"
    for f in p.forms:
        assert f["nested"] == 0, f"form(action={f['action']}) 内有嵌套 form"


def test_all_source_controls_belong_to_settings_form(web_client):
    """每个启用源的 enabled_/quota_/quality_/catalog_url_ 都必须归属 /settings。"""
    markup = web_client.get("/settings").text
    p = _parse_forms(markup)
    settings_form = next((f for f in p.forms if f["action"] == "/settings"), None)
    assert settings_form is not None, [f["action"] for f in p.forms]
    controls = set(settings_form["controls"])
    d = web_client.app_module.get_db()
    with d.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM sources")]
    for sid in ids:
        for prefix in ("enabled_", "quota_", "quality_", "catalog_url_"):
            assert prefix + sid in controls, (
                f"{prefix}{sid} 不在 /settings 表单内（提交时会丢）")


def test_upload_form_carries_its_own_source_and_files(web_client):
    markup = web_client.get("/settings").text
    p = _parse_forms(markup)
    ups = [f for f in p.forms if f["action"] == "/upload-snapshot"]
    assert ups, "缺少快照上传表单（snapshot 馆的自助通道）"
    for f in ups:
        assert "source" in f["controls"] and "files" in f["controls"], f


# =====================================================================
# 2. reader.js：必须写 root.dataset.layout（批 1 第 3 项）
# =====================================================================
def test_reader_js_writes_dataset_layout():
    js = _read("web/static/reader.js")
    hits = len(re.findall(r"root\.dataset\.layout\s*=", js))
    assert hits >= 2, (
        "reader.js 必须在 init() 与 setLayout() 两处写 root.dataset.layout"
        f"（实际 {hits} 处）—— #reader[data-layout=...] 的 CSS 只认它")
    css = _read("web/static/reader.css")
    rules = len(re.findall(r"data-layout", css))
    assert rules >= 4, f"reader.css 应当有依赖 data-layout 的规则（实际 {rules}）"


def test_reader_js_version_bumped_in_template():
    tpl = _read("web/templates/read.html")
    m = re.search(r"reader\.js\?v=(\w+)", tpl)
    assert m, "read.html 必须给 reader.js 带 ?v= 版本号（否则老用户吃缓存）"


# =====================================================================
# 3. XSS 回归守卫：模板不得使用 |safe
# =====================================================================
def test_no_safe_filter_in_templates():
    offenders = []
    for p in sorted(TEMPLATES.glob("*.html")):
        text = p.read_text(encoding="utf-8")
        # 去掉 Jinja 注释 {# ... #} 后再找 |safe
        stripped = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        if re.search(r"\|\s*safe\b", stripped):
            offenders.append(p.name)
    assert not offenders, f"模板里不得出现 |safe（XSS 回归）: {offenders}"


def test_autoescape_enabled(web_client):
    env = web_client.app_module.templates.env
    assert env.autoescape, "Jinja 环境必须开启 autoescape"


# =====================================================================
# 4. data-anns 注入测试
# =====================================================================
PAYLOAD = '<img src=x onerror="alert(1)">'


def test_annotation_payload_is_escaped_in_page(web_client):
    """写入含 <img onerror> 的标注后，/read 页面里必须看不到可执行的标签。"""
    from conftest import write_pdf
    import os
    from core.pipeline import _dest_dir
    d = web_client.app_module.get_db()
    d.upsert_book("loc", {"source_uid": "xss-uid", "collection": "test-set",
                          "title": "XSS", "page_count": 10})
    row = d.find_book("loc", "xss-uid")
    dest = _dest_dir("loc", "test-set", "xss-uid")
    os.makedirs(dest, exist_ok=True)
    write_pdf(os.path.join(dest, "book.pdf"), 1)
    d.set_status(row["id"], "done")
    d.add_annotation(row["id"], 1, "highlight", 0.1, 0.1, 0.4, 0.3,
                     "#ffe066", PAYLOAD)

    page = web_client.get(f"/read/{row['id']}").text
    # 1) 属性注入层面：data-anns 的值必须是合法 JSON 且已做 HTML 转义
    m = re.search(r"data-anns='([^']*)'", page)
    assert m, "read.html 应有 data-anns 属性"
    raw_attr = m.group(1)
    assert "onerror=\"alert(1)\"" not in raw_attr, (
        "属性值里的双引号未转义 → 可跳出属性注入事件处理器")
    decoded = html_mod.unescape(raw_attr)
    data = __import__("json").loads(decoded)
    assert data and any("<img" in (a.get("text") or "") for a in data)
    # 2) 整页层面：不得出现可执行的 <img onerror
    assert "onerror=\"alert(1)\"" not in page.replace("&quot;", '"'), (
        "标注正文被当作 HTML 渲染（XSS）")


def test_export_markdown_escapes_annotation_text(web_client):
    """导出 Markdown 时不得把标注正文原样拼成可执行 HTML。"""
    import os
    from conftest import write_pdf
    from core.pipeline import _dest_dir
    d = web_client.app_module.get_db()
    d.upsert_book("loc", {"source_uid": "xss-md", "collection": "test-set"})
    row = d.find_book("loc", "xss-md")
    dest = _dest_dir("loc", "test-set", "xss-md")
    os.makedirs(dest, exist_ok=True)
    write_pdf(os.path.join(dest, "book.pdf"), 1)
    d.set_status(row["id"], "done")
    d.add_annotation(row["id"], 1, "note", 0.1, 0.1, 0.4, 0.3,
                     "#ffe066", PAYLOAD)
    r = web_client.get(f"/export/annotations/{row['id']}.md")
    assert r.status_code == 200
    assert "<img src=x" not in r.text, "Markdown 导出把正文原样当 HTML 输出"


# =====================================================================
# 5. 其他结构契约
# =====================================================================
def test_book_urls_only_reader_visible_pdfs(tmp_path):
    """book_urls 只认 book.pdf / book_NN.pdf（cover.pdf 不得进阅读器）。

    注意 BOOKS_DIR 有**两份**模块全局：`core.pipeline.BOOKS_DIR`（决定
    `_dest_dir` 落到哪）与 `web.app.BOOKS_DIR`（决定 `rel` 怎么算）。
    两者是各自独立的 str 对象（实测 `pl.BOOKS_DIR is app.BOOKS_DIR` 为 False），
    只 patch 其中一个的话，`_dest_dir` 仍指向真实 data/books，
    这个用例就会在"目录不存在 → pdfs=[]"上失败（不是产品 bug，是 patch 打错地方）。
    """
    import web.app as app_mod
    import core.pipeline as pl
    d = tmp_path
    dest = d / "books" / "loc" / "c" / "u1"
    dest.mkdir(parents=True)
    for n in ("book.pdf", "book_02.pdf", "cover.pdf", "u1_書名.pdf"):
        (dest / n).write_bytes(b"%PDF-1.4\n")
    old_app, old_pl = app_mod.BOOKS_DIR, pl.BOOKS_DIR
    try:
        app_mod.BOOKS_DIR = pl.BOOKS_DIR = str(d / "books")
        urls = app_mod.book_urls({"source_id": "loc", "collection": "c",
                                  "source_uid": "u1"})
    finally:
        app_mod.BOOKS_DIR, pl.BOOKS_DIR = old_app, old_pl
    names = sorted(u.rsplit("/", 1)[-1] for u in urls["pdfs"])
    assert names == ["book.pdf", "book_02.pdf"], names


def test_templates_referenced_by_app_exist(web_client):
    app_mod = web_client.app_module
    names = set()
    for p in TEMPLATES.glob("*.html"):
        names.add(p.name)
    assert "bookshelf.html" in names and "read.html" in names
    assert os_path_exists(app_mod.templates.env.loader.searchpath[0])
    assert app_mod.templates


def os_path_exists(p):
    import os
    return os.path.isdir(p)
