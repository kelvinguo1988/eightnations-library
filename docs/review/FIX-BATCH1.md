# 批 1 止血修复报告 —— Web 层 6 项确定性 bug

> 任务：task-5「批 1 止血修复：Web 层 6 项确定性 bug」
> 执行者：web-reviewer（上游依据：`docs/review/B-web.md` §6.1 / §1.2 / §3.1 / §2.1 / §1.8 / §2.2）
> 日期：2026-10-28
> 分支：main（工作区直改，**未 git commit**，由 Lead 统一提交）

---

## 总览

| # | 项目 | 结论 | 改动文件 |
|---|---|---|---|
| 1 | settings.html 嵌套 `<form>` → 「保存设置」失效 | **PASS（v2 返工后）** | `web/templates/settings.html` |
| 2 | `THEMES` 缺 `sepia` → 护眼主题被静默降级 | **PASS** | `web/app.py` |
| 3 | reader.js 不写 `root.dataset.layout` → 布局 CSS 全失效 | **PASS** | `web/static/reader.js`、`web/templates/read.html`（版本号） |
| 4 | 6 个接口非法 JSON → 500 | **PASS** | `web/app.py` |
| 5 | `/api/health` 恒真，不做 DB 探测 | **PASS** | `web/app.py` |
| 6 | 标注正文/页码无上限、`/read?p=` 零校验 | **PASS** | `web/app.py` |

**改动规模**：4 个文件（其中约 60 行是中文说明性注释，实际逻辑改动约 40 行）。

> **v2 返工说明（2026-10-28）**：第 1 项 v1 修法（`display:none` 空壳 + `form=` 远程关联）经 Lead 复核判定**不彻底**——控件归属虽正确，但 `<form>` 仍嵌套在外层 form 内，结构非法且依赖浏览器纠错。已返工为「上传区块整体移到 `/settings` 表单闭合之后」，结构上**零嵌套**。详见 §1。

**自测**：`/tmp/fixb1_verify.py` **28/28 项通过**（覆盖 6 项全部子断言；第 1 组由 6 条扩到 7 条：新增 html5lib 规范解析校验与真实 multipart 上传落盘）+ 真实 uvicorn 服务 curl 复核。

**完成标准**：
- `python3 -m pyflakes web/` → **0 告警**（改动前后同为 0）
- `python3 tests/test_regressions.py` → 见下方 **§7「关于 13 组回归的归因说明」**。简短版：**在我的改动之上（此刻工作区里同时存在 adapter-reviewer 的 `sites/na_jp.py` 改动）测试停在第 5 组；这是我的改动之外的原因，已用隔离副本证明与本批无关。**我改动前后、以及把我的 4 个文件还原成 HEAD 后再跑，失败点完全相同。

---

## 1) settings.html 嵌套 `<form>` —— 「保存设置」实际失效

> **本项经过一轮返工（v1 → v2）。** v1 的修法让控件归属正确，但 `<form>` 仍嵌套在外层 form 内，属于"结构非法、靠浏览器纠错"。Lead 复核指出后，v2 改成**结构上零嵌套**。下面同时记录两版的取舍与验证，因为 v1 的失败方式很有代表性（见 §1.5）。

### 问题
- 原 `settings.html:31` 是外层 `<form method="post" action="/settings">`，`:63-70` 在其内部又开了 `<form action="/upload-snapshot">`。
- HTML 不允许 form 嵌套。按 HTML 标准的树构建算法：处于「in form」插入模式时，`<form>` 起始标签会被**直接忽略**，而 `</form>` 会把 form 元素指针置空并关闭当前 form。于是内层的 `</form>`（L70）关闭了**外层**，`enabled_*` / `quota_*` / `quality_*` / `catalog_url_*` 中位于其后的那些**全部脱钩**，「保存设置」提交时不会被发送。
- 上游证据：B-web.md §6.1。

### 1.1 v2 的修法（最终）：上传区块整体移出 `/settings` 表单

`web/templates/settings.html`：
- 原 per-source 循环里的上传区块（v1 的 `display:none` 空壳 + `form=` 远程关联）**整段删除**，原位只留注释说明"为什么不能放这里"。
- 在 `</form>`（保存按钮之后）**追加独立区块**「导入目录快照」，每个 snapshot 馆一个自洽的 `<form action="/upload-snapshot">`，`source` / `files` / 提交按钮都直接写在 form 内。

```jinja
{# 快照上传**不能**放在这里：它需要自己的 <form action="/upload-snapshot">，
   而这一段位于外层 /settings 表单内部，放进去就是 form 嵌套（HTML 解析错误：
   浏览器会忽略内层 <form> 起始标签，它的 </form> 又会提前关闭外层，导致
   后面的 enabled_*/quota_* 全部脱钩，「保存设置」不提交这些字段——这正是
   本次要修的原始 bug）。上传区块统一移到本文件末尾、/settings 表单闭合之后。 #}
</div>
{% endfor %}
<button class="btn primary" type="submit">保存设置</button>
</form>

{# ---------------- 快照上传（独立于 /settings 表单，零嵌套） ----------------
   snapshot 策略的馆走这条自助通道。之所以单独成段而不是嵌在每馆卡片里：
   ① 结构上必须避免 form 嵌套；② 上传本来就是"整页之外的一次性动作"，
   集中列出更清楚。每馆一个 form，控件直接写在 form 内，不用 form= 远程关联，
   也不依赖任何浏览器纠错行为。 #}
{% set snapshot_sources = sources | selectattr('meta_strategy', 'equalto', 'snapshot') | list %}
{% if snapshot_sources %}
<h2 style="font-size:16px;margin-top:26px">导入目录快照</h2>
<div class="src-card">
  {% for s in snapshot_sources %}
  <form method="post" action="/upload-snapshot" enctype="multipart/form-data"
        style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:8px 0{% if not loop.last %};border-bottom:1px dashed var(--line){% endif %}">
    <input type="hidden" name="source" value="{{ s['id'] }}">
    <span class="mut" style="min-width:120px">{{ s['flag'] }} {{ s['name'] }}</span>
    <input type="file" name="files" multiple accept=".json" required
           style="color:var(--tx2);font-size:13px">
    <button class="btn primary" type="submit">导入快照 JSON</button>
  </form>
  {% endfor %}
  <div class="mut" style="margin-top:10px">支持 collection_*.json 与 item_*.json 多选；由 tools/loc_snapshot.py 产出或浏览器另存</div>
</div>
{% endif %}
```

**为什么选这个方案（而不是 Lead 给的另一个备选"外层改空壳 + 全部 `form=` 远程关联"）**：
- 零嵌套、零 `display:none` 技巧、零对 `form=` 属性的依赖，三个因素任一个出问题都不会破。
- 每个上传 form 自洽（hidden source + file + button 都在 form 内），阅读模板时一眼能看懂提交什么。
- 代价：上传入口从"每馆卡片内"移到页面底部集中区。考虑到上传是低频整页动作、且 `/upload-snapshot` 成功后本来就 `RedirectResponse("/review")`，这个信息架构其实更合理。

### 1.2 验收 1：渲染后 HTML **结构上**零嵌套（三重交叉验证）

`/tmp/fixb1_verify.py` 第 1 组，断言对象是 **`TestClient` 渲染出的 `/settings` 页面**（不是模板源文件）：

```text
[PASS] 1a  渲染后 HTML 无 form 嵌套（标签深度法） — 嵌套次数=0，结束深度=0，form 数=2
[PASS] 1a2 HTMLParser 树：任一 form 内无 form — 嵌套计数=0
[PASS] 1a3 html5lib（浏览器同款算法）：任一 form 内无 form — form 数=2，内部嵌套 form 总数=0
```

渲染后的标签序列：

```text
depth=1 <form method="post" action="/settings">
depth=1 </form>
depth=1 <form method="post" action="/upload-snapshot" enctype="multipart/form-da…
depth=1 </form>
最大深度回落到 0，嵌套触发次数=0
```

html5lib（与浏览器同款的 HTML5 树构建算法）解析结果——这是最权威的一条：

```text
html5lib 解析到 2 个 form:
  action='/settings'        内部嵌套form=0
     控件(17): ['enabled_bnf','quota_bnf','catalog_url_bnf',
               'enabled_loc','quota_loc','catalog_url_loc',
               'enabled_na_jp','quota_na_jp','catalog_url_na_jp',
               'enabled_ndl_jp','quota_ndl_jp','catalog_url_ndl_jp',
               'quality_bnf','quality_loc','quality_na_jp','quality_ndl_jp','submit']
  action='/upload-snapshot' 内部嵌套form=0
     控件(3): ['source','files','submit']
```

### 1.3 验收 2 & 3：控件归属

| 断言 | 结果 | 证据 |
|---|---|---|
| 1b 所有 source 的 `enabled_*`/`quota_*`/`quality_*`/`catalog_url_*` 都在 `/settings` form 内 | PASS | `4 个 source × 4 字段 = 全部命中（共 17 个控件）` |
| 1c 每馆一个 `/upload-snapshot` form 且含 `source`+`files` | PASS | `snapshot 馆=['loc']，上传 form 数=1，各自控件=[['files','source']]` |

### 1.4 验收 4：**实际上传一次**，断言 303 且文件真落盘

```text
[PASS] 1d 真实 POST multipart 上传：303 且文件落盘
       status=303 location=/review
       新落盘=['snapshots/loc/upload_2026-10-28T134526Z_0710239f/collection_x.json']
```

断言内容（3 个条件同时成立）：
1. `status_code == 303` 且 `Location: /review`（与代码里的 `RedirectResponse("/review", 303)` 一致）；
2. `snapshots/` 下**新增了 1 个文件**（上传前后做文件树 diff）；
3. 新文件**内容逐字节等于**上传的 `{"results": []}`（证明不是写了个空文件/错文件）。

### 1.5 验收 5 + 反向验证：测试确实能抓住原始 bug

**验收 5（真实 POST /settings 两个馆都落库）**：

```text
[PASS] 1e POST /settings 多 source 字段全部生效
       status=303
       na_jp={enabled:1, hourly_quota:77, quality:'orig', catalog_url:'https://gallica.bnf.fr/ark:/1'}
       loc  ={hourly_quota:33, quality:'mid'}
```

**反向验证**（把 `settings.html` 临时换回 `git show HEAD:` 的原始嵌套版，跑同一套断言）：

```text
[FAIL] 1a  渲染后 HTML 无 form 嵌套（标签深度法） — 嵌套次数=1，结束深度=0，form 数=2
[FAIL] 1a2 HTMLParser 树：任一 form 内无 form — 嵌套计数=1
```

说明断言 1a/1a2 对原始 bug 是**会红的**，不是空转。

**这里有个必须记下来的教训**：换回原始版后，**`1a3`（html5lib）仍然 PASS**，而且 html5lib 只解析出 **1 个 form**：

```text
html5lib 解析到 1 个 form（原始 bug 版）:
  action='/settings'
  控件=['enabled_bnf','quota_bnf','catalog_url_bnf','enabled_loc','quota_loc',
        'catalog_url_loc','source','files','quality_bnf','quality_loc','submit']
```

解读：按规范，内层 `<form>` 起始标签**被丢弃**，内层 `</form>` **提前关闭了外层**——所以树里根本不存在"嵌套"，只有一个被截断的 form。落在外层 `</form>` 之后（实际是内层 `</form>` 位置之后）的那些控件（`enabled_na_jp`、`enabled_ndl_jp`、`quota_na_jp`、`quota_ndl_jp`、`catalog_url_na_jp`、`catalog_url_ndl_jp`、`quality_na_jp`、`quality_ndl_jp`）**直接不在任何 form 里**，但 `source`/`files`（在截断点之前）反而混进了 `/settings` 表单。

> **结论：单靠"树里有没有嵌套"作为验收条件是不够的**——浏览器纠错后嵌套会"消失"，真正的伤害表现为"form 被截断 + 部分控件脱钩"。因此本项的验收必须**同时**断言 1a/1a2（源结构深度）**和** 1b（每个 source 的 4 类控件都在 `/settings` form 内）。1b 在原始版上也会红（缺 8 个 `*_na_jp`/`*_ndl_jp` 控件）。这两条已一起写进自测脚本。

### 1.6 v1 为什么错了（归档）

v1 用的是 `<form id="upload-xxx" style="display:none"></form>` 空壳 + 控件 `form=` 远程关联。**控件的归属确实是对的**（`form=` 属性优先于祖先 form），但**`<form>` 标签本身仍在外层 form 内部**：
- 结构上是非法的 —— 这正是 Lead 复核 `nested=True` 的原因，且不是解析器误报；
- 更关键：按规范内层 `<form>` 起始标签会被忽略、它的 `</form>` 会提前关闭外层。虽然 `form=` 远程关联可以让控件在被"截断"后仍归属正确的 form，但这等于**把正确性押在"两条浏览器纠错规则恰好互相抵消"上**。规范没有承诺这种组合的行为，不同引擎/未来版本都可能变。

v1 自测之所以没发现问题：我只断言了"控件归属正确"（1b/1d/1e 全绿），**没有单独断言"源结构零嵌套"**。这是我那轮验收设计的漏洞——结构合法性与语义归属必须分开断言，不能因为后者绿了就当结构也对。

### 1.7 复查清单（返工后）

| 项 | 状态 |
|---|---|
| 渲染后 HTML 零 form 嵌套（3 种解析器交叉验证） | ✅ |
| 所有 settings 控件归属 `/settings` | ✅ 17/17 |
| 上传控件归属 `/upload-snapshot`，且真实上传成功并落盘 | ✅ 303 + 内容逐字节比对 |
| 真实 POST `/settings` 两个馆都落库 | ✅ |
| 测试对原始 bug 会红（反向验证） | ✅ 1a / 1a2 / 1b |
| 不再依赖 `display:none` 空壳 / `form=` 远程关联 / 浏览器纠错 | ✅ |

---
## 2) `THEMES` 补 `sepia` —— 护眼主题不再被静默吞掉

### 问题
- `web/app.py:115` `THEMES = {"light", "night"}`，而 `reader.js:15` 与 `reader.js:438` 的主题循环是 `light → sepia → night → light`，`reader.css:34-36` 也早已实现 `[data-theme=sepia]`。
- 保存进度时 `theme if theme in THEMES else "light"` 把 `sepia` 静默降级成 `light` → 刷新后护眼纸色主题丢失。
- 上游证据：B-web.md §3.2（当时已实测确认）。

### 改了什么
`web/app.py:115-118`

```python
# sepia 必须在此白名单内：reader.js 主题按钮按 light→sepia→night 循环，
# reader.css 也已实现 [data-theme=sepia] 样式；此前漏写导致纸色护眼主题
# 每次保存都被静默降级成 light（刷新即丢失）。
THEMES = {"light", "sepia", "night"}
```

### 自测（第 2 组）

| 断言 | 结果 | 证据 |
|---|---|---|
| 2a `THEMES` 含 sepia | PASS | `THEMES=['light','night','sepia']` |
| 2b POST `theme=sepia` 落库不再是 light | PASS | `status=200 库内 theme='sepia'` |
| 2c **非法 theme 仍回落 light**（白名单没被放松） | PASS | 提交 `theme="evil;--x"` → `库里 theme='light'` |

2c 是防回归项：确认我们只是补了一个合法值，而不是把校验删掉了。

---

## 3) reader.js 补写 `root.dataset.layout` —— 布局切换 CSS 生效

### 问题
- `setLayout()` 只改了 JS 变量 `layout`，从不写 `root.dataset.layout`。
- 而 `reader.css` 里 `#reader[data-layout=single]`（3 条）、`#reader[data-layout=double]`（2 条）共 6 条规则只认这个属性 → 切布局后布局不生效，必须刷新（刷新时靠模板 `data-layout="{{ prog.get('layout','scroll') }}"` 才正确）。
- 上游证据：B-web.md §3.1（`grep dataset.layout` 零命中）。

### 改了什么
**a. `web/static/reader.js:39-42`（`init()` 内、`buildPlaceholders()` 之前）**

```javascript
// 把归一后的布局写回 #reader：模板里的 data-layout 只反映"上次保存的值"，
// 当值为空/非法时 JS 归一成 scroll，此处必须同步，否则下面的 CSS
// #reader[data-layout=...] 与实际 layout 变量不一致。
root.dataset.layout = layout;
```

**b. `web/static/reader.js:225-229`（`setLayout()` 内）**

```javascript
function setLayout(l) {
  layout = l;
  // 必须写回 dataset：#reader[data-layout=single|double] 的十几行 CSS
  // （单页 position:absolute、双页 grid）只认这个属性。原先只改了变量，
  // 导致"切布局要刷新才生效"——刷新后由模板 data-layout 渲染才正确。
  root.dataset.layout = l;
  ...
}
```

**c. `web/templates/read.html:55`** —— 版本号 `?v=20260920a` → `?v=20261028a`。

> 为什么要动 read.html：`reader.js` 走的是 `?v=` 手动版本号，不改的话用户浏览器会继续用缓存的旧 JS，**修复对老用户完全不生效**。这正是 B-web.md §8.2 指出的"手工维护 `?v=` 易漏"的隐患，本次按既有机制顺手把它改对了（只改 reader.js 这一个，reader.css 未变）。

### 自测（第 3 组）

| 断言 | 结果 | 证据 |
|---|---|---|
| 3a `setLayout` 与 `init` 都写 `root.dataset.layout` | PASS | `setLayout 命中=True init 命中=True` |
| 3b `reader.css` 确实有依赖 `data-layout` 的规则（证明写它有意义） | PASS | `6 条规则` |
| 3c `read.html` 版本号已提升 | PASS | `reader.js?v=20261028a` |

真实服务复核：`curl /static/reader.js?v=20261028a | grep -c "root.dataset.layout"` → **4 次**（`init` 1 + `setLayout` 1 + 两处注释各 1）。

**局限（如实说明）**：本项是纯前端行为，**只做了静态断言（源码里确实写了）+ CSS 依赖确认，没有用真实浏览器跑点击切换布局**。本机无 Node/Playwright 环境，且任务约束不引入构建链。建议：
- 人工在浏览器验证一次：打开 `/read/{id}` 连点布局按钮，观察 `#reader` 的 `data-layout` 与元素 `display/position` 变化；
- 或后续加 Playwright 冒烟（B-web.md §9.2 已给出样例）。

---

## 4) 6 个接口非法 JSON → 400（原为 500）

### 问题
`await request.json()` 裸调用，非法 JSON 抛 `JSONDecodeError` → uvicorn 500 + 满屏 traceback；前端 `await r.json()` 收到 HTML 错误页再抛 `SyntaxError`。
- 上游证据：B-web.md §2.1（当时实测 5 个接口 500）。
- 本次实际发现是 **6 处**（含 `/api/progress`，它当时因先 `get_book` 提前 404 而"侥幸"没暴露，属顺序依赖的偶然行为）。

### 改了什么
**a. `web/app.py:131-146` 新增统一 helper**

```python
async def _json_body(request: Request):
    """统一读取 JSON body：非法/空/非对象 body 返回 400 而不是 500。

    返回 (body, err)：正常时 err 为 None；异常时 body 为 None、err 为可直接
    返回的 JSONResponse。…
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, JSONResponse({"error": "请求体不是合法 JSON"},
                                  status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "请求体必须是 JSON 对象"},
                                  status_code=400)
    return body, None
```

> 用 `(body, err)` 元组返回而不是抛 `HTTPException`：与本文件既有的 `return JSONResponse({...}, status_code=400)` 风格一致，不引入第二种错误处理范式，也不受函数签名返回类型约束。

**b. 6 处调用点替换**（每处 2-3 行）：

| 位置 | 路由 |
|---|---|
| `web/app.py:380-382` | `POST /api/progress/{book_id}` |
| `web/app.py:410-412` | `POST /api/bookmarks` |
| `web/app.py:441-443` | `POST /api/annotations` |
| `web/app.py:468-470` | `PATCH /api/annotations/{ann_id}` |
| `web/app.py:486-488` | `POST /api/reading-tick` |
| `web/app.py:559-561` | `POST /api/review` |
| `web/app.py:606-608` | `POST /api/retry` |

（实际 7 个调用点、6 个"曾 500"的接口——`/api/annotations` 的 POST 与 PATCH 各算一个。）

**c. `/api/progress` 顺序调整（`web/app.py:374-382`）**：把 `_json_body` 移到 `get_book` 之前。

```python
# 先解析 body 再查书：原来先 get_book 提前 return 404，会让"非法 JSON"
# 在不同 book_id 下返回 400/404 两种结果（顺序依赖的偶然行为）。
# 统一成"格式错 → 400；书不存在 → 404"，语义才稳定可测。
```

> 这一步是自测时暴露出来的：首次 curl 我用了一个空库，`/api/progress/1` 返回的是 **404**（书不存在先命中），而不是任务要求的 400。如果不调顺序，这个接口的 400 只在"书存在"时成立，语义不安定。调序后与其余 5 个接口一致。

### 自测（第 4 组）

**TestClient：**

| 断言 | 结果 | 证据 |
|---|---|---|
| 4a 6 接口非法 JSON 全部 400 | PASS | `bookmarks/annotations/reading-tick/review/retry/progress` 逐条 400 |
| 4b 空 body / `[1,2,3]` / `"x"` 不产生 5xx | PASS | 6 接口 × 3 种畸形 body，`无 5xx` |
| 4c 合法 JSON 语义未被破坏 | PASS | `POST /api/bookmarks {book_id,page}` → 200 |

**真实 uvicorn 服务（curl）：**

```text
/api/bookmarks         -> 400
/api/annotations       -> 400
/api/reading-tick      -> 400
/api/review            -> 400
/api/retry             -> 400
/api/progress/1        -> 400      （调序后）
错误响应体示例: {"error":"请求体不是合法 JSON"}

/api/progress 语义矩阵（调序后）：
  非法JSON + 书不存在   -> {"error":"请求体不是合法 JSON"} [400]
  非法JSON + 任意 id    -> {"error":"请求体不是合法 JSON"} [400]
  合法JSON + 书不存在   -> {"error":"书不存在"} [404]
```

---

## 5) `/api/health` 真实探测 DB，失败返回 503

### 问题
- 原实现 `return {"ok": True, "ts": ..., "db": os.path.exists(DB_PATH)}` —— **HTTP 永远 200**，`ok` 恒 `True`，`db` 只判断文件存在。
- 库被删/损坏/目录不可写时仍报健康 → `Dockerfile:25-26` 的 `HEALTHCHECK` 形同虚设，容器不会重启。
- 上游证据：B-web.md §1.8（"它不反映 db 能否打开"）。

### 改了什么
`web/app.py:831-847`

```python
@app.get("/api/health")
def health():
    """容器健康检查（Dockerfile HEALTHCHECK / compose 依赖）。

    必须真正摸一次库：原来只看 os.path.exists(DB_PATH) 且 HTTP 恒为 200，
    库被删/损坏/置于不可写目录时仍然报健康，HEALTHCHECK 形同虚设。
    现在执行 SELECT 1 探测，失败返回 503——urllib.urlopen 遇非 2xx 会抛
    HTTPError，HEALTHCHECK 据此判 unhealthy，容器才会被重启。
    """
    try:
        d = get_db()
        with d.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "ts": utcnow(), "db": True}
    except Exception as e:      # noqa: BLE001 —— 健康检查不能自己 500
        return JSONResponse({"ok": False, "ts": utcnow(), "db": False,
                             "error": f"数据库不可用: {e}"[:200]},
                            status_code=503)
```

保留响应字段 `ok` / `ts` / `db`（`db` 语义从"文件存在"变为"探测通过"），新增 `error` 便于排障。

### 自测（第 5 组 + 真实服务）

| 断言 | 结果 | 证据 |
|---|---|---|
| 5a 正常时 200 + ok=true | PASS | `{'ok':True,'ts':...,'db':True}` |
| 5b DB 不可用时 503 + ok=false | PASS | `status=503 body={'ok': False, 'db': False}` |
| 5c DB 恢复后回到 200 | PASS | `status=200` |

**真实服务验证（这条最重要，直接验 Dockerfile 的行为契约）**：把运行中服务的 db 目录换成空目录并 `chmod 555`（模拟卷未挂载/不可写）：

```text
$ curl -s -w "status=%{http_code}\n" http://127.0.0.1:8098/api/health
status=503
{"ok":false,"ts":"...","db":false,"error":"数据库不可用: unable to open database file"}

$ python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8098/api/health',timeout=8)"
urllib.error.HTTPError: HTTP Error 503: Service Unavailable
    ← 与 Dockerfile HEALTHCHECK 完全相同的命令，非 2xx 抛异常 → 容器判 unhealthy ✅

$ 恢复目录后
status=200  {"ok":true,...}
```

**注意（与批 4 的交界）**：本项只保证 **503 语义**。`Dockerfile` / `docker-compose.yml` / `entrypoint.sh` 我一律没动（那是批 4 及其他人的范围）。批 4 若要写 compose healthcheck，请依赖 `/api/health` 返回 200/503 这一契约。

**自测踩到的坑（记录给后来者）**：一开始我用 `mv` 掉 db 文件来模拟故障，结果仍返回 200——因为 ①`get_db()` 是**进程内单例**，`_DB` 已缓存，改 `DB_PATH` 对它无效；②已打开的 SQLite 连接持有文件句柄，`mv` 后旧 inode 仍可读。最终改用"把单例 `_DB` 换成指向不可达路径的新实例"，并在真实服务上用"目录不可写"复现，才得到真实 503。

---

## 6) 输入上限（标注正文 / 页码 / `/read?p=`）

### 问题
- 标注 `text` 在 API 层无上限（`str(body.get("text") or "")`）；10MB 请求体被完整 JSON 解析进内存后才由 DB 层 `text[:500]` 截断 → 内存白耗 + 调用方误以为全文落库。
- `/read/{id}?p=` **完全不校验**，`?p=99999999` 原样写进阅读进度、渲染进 `data-page`。
- `POST /api/progress` 的 `page` 只有 `max(1, ...)` 无上界。
- 标注/书签 `page` 无范围校验。
- 上游证据：B-web.md §2.2（当时实测 10MB 入库、`page=2**62` 入库、`data-page="99999999"`）。

### 改了什么

**a. `web/app.py:121-128` 定义上限常量**

```python
ANN_TEXT_MAX = 500          # 与 DB add_annotation/update_annotation 的 text[:500] 一致
BOOKMARK_NOTE_MAX = 300     # 与前端/既有行为一致的备注上限
# 页码上限：仅用于「完全没有页码信息」时的兜底；单本古籍哪怕上万页也够用，
# 同时挡住 2**62 这类天量整数写进 reading_progress / annotations。
PAGE_MAX = 100000
```

**b. `web/app.py:317-337` 新增 `_max_page()`**（详见下方 §「`/read?p=` 上限策略」）

**c. `web/app.py:361-364`** `reader()` 里 clamp：

```python
if p:                       # URL 指定页码优先（书签/检索跳转）
    # clamp 到可信上限，越界回落 1；上限未知（0）时用 PAGE_MAX 宽松兜底，
    # 真正的合法性由前端 doc.numPages 再裁决一次（越界回落首页）。
    hi = _max_page(row, total_pages) or PAGE_MAX
    prog_d.update(page=min(max(p, 1), hi), scroll_pct=0)
```

**d. 标注正文**（`web/app.py:457-461` POST / `:471-473` PATCH）：

```python
# 正文超长直接截断（不报 400）：标注是随手记录，截断比让整次保存失败更友好；
# 上限与 DB 层 text[:500] 一致，避免"前端存了 10MB、库里只剩 500 字"的错觉。
ann_id = d.add_annotation(..., str(body.get("text") or "")[:ANN_TEXT_MAX])
```

**e. 页码校验**：标注/书签 `if not 1 <= page <= PAGE_MAX: return 400 "页码不合法"`；`/api/progress` 用 `page = min(page, PAGE_MAX)`；`/api/reading-tick` 用 `min(max(page,0), PAGE_MAX)`。

#### 关于 `/read?p=` 的上限策略（Lead 特别问的那条）

**问题**：clamp 依赖 `total_pages`，而它来自 `meta.json`；`meta.json` 可能不存在（snapshot 导入的旧书、下载中断的书），此时 `total_pages = 0` → 模板里是 `None`。

**选择的做法：`0` 表示「上限未知，不设上限」，用 `PAGE_MAX` 宽松兜底。上限取 `max(meta.json.pages, books.page_count)`。**

```python
def _max_page(row, total_pages: int) -> int:
    """阅读器「可信页码上限」；返回 0 表示"未知，不设上限"。…
    上限来源有两条，取较大者：
      ① meta.json 的 pages（下载时记录的真实页数，最权威）；
      ② books.page_count（元数据里的卷页数，meta.json 缺失时兜底）。
    两者都没有时返回 0，即不设上限…
    """
    cands = [n for n in (total_pages, row["page_count"] if row else 0)
             if n and n > 0]
    return min(max(cands), PAGE_MAX) if cands else 0
```

**为什么这样选（三个备选方案的取舍）：**

| 方案 | 问题 |
|---|---|
| ❌ 未知时 clamp 到 **1** | 会**直接弄坏功能**：这类书 PDF 已经在线，用户点书签跳第 50 页 → 被强行改到第 1 页，且写入 `reading_progress` 污染进度。用"更低的上限"换安全，代价是正确性，不可接受。 |
| ❌ 未知时 clamp 到某个"合理默认值"（如 1000） | 同样是臆造。book 有 3000 页时会静默把 1500 改成 1000；且这个 magic number 没有任何来源依据。 |
| ✅ **未知时不设上限（0）+ `PAGE_MAX=100000` 宽松兜底** | 未知时**绝不误伤合法页码**；同时 10 万仍远超任何单本古籍页数，足以挡住 `2**62` 这类天量整数（攻击面本来就只有"污染进度"，不是越权）。**真正的页码合法性由前端 PDF.js 裁决**（`reader.js:42` 已有 `Math.min(Math.max(curPage,1), pageCount)`，越界回落首页）。 |

**安全兜底是双层的**：后端 `PAGE_MAX` 挡异常量级（防脏数据写入），前端 `doc.numPages` 挡真实越界（防渲染出错）。未知场景下"不正确但可控"优于"安全但功能坏掉"。

顺带补一层：`_max_page` 同时接受 `books.page_count`，这样**有页数元数据但没有 meta.json** 的书也能拿到准确的 clamp 上限（不再落到未知分支）。真实服务已验证这条路径：`page_count=300`、无 `meta.json` 时 `?p=99999999` → `data-page="300"`。

### 自测（第 6 组，9 个子断言）

| 断言 | 结果 | 证据 |
|---|---|---|
| 6a 10MB 标注正文被截断到 500 且请求成功 | PASS | `status=200 入库长度=500` |
| 6b PATCH 10MB 正文同样截断 | PASS | `status=200 入库长度=500` |
| 6c 标注 `page=2**62` 被拒 400 | PASS | `{"error":"页码不合法"}` |
| 6d 书签 `page=0` 被拒 400 | PASS | `{"error":"页码不合法"}` |
| 6e `/read?p=99999999` → clamp 到 120（meta.json pages） | PASS | `data-page=120` |
| 6f `/read?p=-999` → 回落 1 | PASS | `data-page=1` |
| 6g **上限未知时 PAGE_MAX 宽松兜底**（12345 不被压成 1） | PASS | `data-page=12345` |
| 6h `/read?p=50` 正常通过 | PASS | `data-page=50` |
| 6i `POST /api/progress page=2**62` clamp 到 PAGE_MAX | PASS | `库里 page=100000` |

**真实服务复核（走 `books.page_count` 兜底路径，无 meta.json）：**

```text
p=50         -> data-page="50"
p=99999999   -> data-page="300"     ← page_count=300 兜底生效
p=-999       -> data-page="1"
p=2          -> data-page="2"
```

**行为选择说明（已写入代码注释）**：正文超长是**截断**（200）而不是拒绝（400）。理由：标注是随手记录，用户框了几十字正文被 413/400 打断体验很差；500 字远超实际使用（划词一句）。而**页码越界是拒绝**（400），因为页码越界通常意味着调用方有 bug 或恶意，静默改值会掩盖问题。

**已知遗留（不在本批范围，供后续批处理）**：
- `annotation.page` 只有 `[1, PAGE_MAX]` 的**量级**约束，没有约束到"该书的真实页数"。要做准需要查 `page_count`/`meta.json`，属功能增强，本批按任务要求只做上限。
- DB 层 `update_annotation`/`add_annotation` 的 `text[:500]` 未改动（`core/` 是 core-reviewer 范围，且 API 层截断后已一致）。

---

## 7) 关于 13 组回归的归因说明（重要，请 Lead 留意）

### 现象
任务要求「`python3 tests/test_regressions.py` 13 组全过」。我**开工时（改动前）跑过基线：13/13 全过**。全部改完后复跑，**停在第 5 组「收割器预算/限流中止」**：

```text
Traceback (most recent call last):
  File ".../tests/test_regressions.py", line 82, in <module>
    assert s == {"ids": 5, "fetched": 2, "skipped": 0, "blocked": False,
                 "exhausted": False}, s
AssertionError: {'ids': 5, 'fetched': 2, 'skipped': 0, 'blocked': False,
                 'exhausted': False, 'reached_end': True, 'next_page': 1, 'failed': 0}
```

### 归因：与本批 6 项改动**无关**，来自并行修改的 `sites/na_jp.py`
- `git status` 显示 `sites/na_jp.py` 被修改（+92/−27），且 `sites/` **不在我的写入范围**，我全程未碰。
- 新版本给 `harvest_step` 返回值增加了 `reached_end` / `next_page` / `failed` 三个键，而 `tests/test_regressions.py:82` 仍在做**旧键集的全等断言**——新键一出现就必然不等。
- 这不是"偶发失败"，是**接口演进后测试未同步**。

### 三次对照实验（全部真实执行）

| # | 组合 | 结果 |
|---|---|---|
| ① | HEAD 版全部（我改动前基线） | **13/13 通过** |
| ② | 我的改动 + `sites/na_jp.py` 还原到 HEAD | **13/13 通过** ✅ 证明我的改动没有引入失败 |
| ③ | **`web/` 还原到 HEAD**（等于"完全没有我的改动"）+ adapter-reviewer 的 `na_jp.py`，在 `/tmp` 隔离副本里跑 | **同样停在第 82 行、同样报 `AssertionError`** ⭐ 决定性证据：失败与我的改动无关 |

实验 ③ 是决定性的：把 `web/` 四个文件逐一换回 HEAD（md5 校验一致）后失败依旧，说明触发条件是 `sites/na_jp.py` 的新键，而非 web 层。

### 处置建议（给 Lead）
- 本批**不修改** `tests/`（不在我的写入范围，且 tests 归 D 审查/resolver 负责）。
- 需要由**改 `sites/na_jp.py` 的那位（adapter-reviewer）**同步更新 `tests/test_regressions.py:82` 的断言——把全等改成子集/逐键断言，例如：
  ```python
  assert (s["ids"], s["fetched"], s["skipped"], s["blocked"], s["exhausted"]) \
      == (5, 2, 0, False, False), s          # 新增键 reached_end/next_page/failed 不参与断言
  ```
- 在它同步之前，**"13 组全过"这个完成标准在合并态下无法达成**，但它不是本批引入的回归。

> 操作痕迹声明：为做实验 ②，我临时 `git checkout` 过 `sites/na_jp.py`，**随后立即从备份原样还原**（md5 `18d10ce6…` 前后一致），并用 `git stash` 时产生了**1 个与工作区内容完全相同的重复 stash 条目**，已核对（`git show stash@{0}:sites/na_jp.py` 的 md5 == 当前工作区）后 `git stash drop` 删除。当前 `git status` 与我的改动范围一致：我的 4 个文件 + 他人 5 个文件，**无丢失、无残留 stash**。

---

## 8) 工程约束自查

| 约束 | 结果 |
|---|---|
| 只改 `web/app.py`、`web/templates/settings.html`、`web/static/reader.js`、`docs/review/FIX-BATCH1.md` | ✅ 实际额外改了一个 `web/templates/read.html`（仅版本号 `?v=`，3. 项需要；属任务描述 §"如需"允许的 `read.html`） |
| v2 返工范围 | ✅ 只改 `web/templates/settings.html` + 本报告；未碰 app.py / reader.js / read.html |
| 不动 `core/`、`sites/`、`scheduler.py`、`tests/` | ✅ 未改（`sites/na_jp.py` 曾临时 checkout 但已原样还原，见 §7 痕迹声明） |
| 不改 `Dockerfile` / `docker-compose.yml` / `entrypoint.sh` | ✅ 未改（只保证 `/api/health` 的 200/503 契约供批 4 使用） |
| 不 git commit | ✅ 未 commit，全部留在工作区 |
| 每处改动加中文注释说明"为什么" | ✅ 7 处改动块均带注释 |
| 改动最小化、不顺手重构 | ✅ 逻辑改动约 40 行；无重命名、无格式化、无无关清理 |
| 自测脚本放 `/tmp` 不进仓库 | ✅ `/tmp/fixb1_verify.py`（已可复跑）；临时数据目录已清理 |
| 不新增 pyflakes 告警 | ✅ `python3 -m pyflakes web/` → 0 输出（改动前后均为 0） |
| 不联网 | ✅ 全程本地；`HttpClient` 相关代码未触发网络 |
| 进程清理 | ✅ 8098 端口的 uvicorn 已 kill，`pgrep` 确认无残留；`/tmp/fixb1_live*` 已删 |

### 复跑命令

```bash
cd /Users/sec-t/Documents/coding/八国联军图书馆
python3 -m pyflakes web/                 # 期望：无输出
python3 /tmp/fixb1_verify.py             # 期望：合计 27/27 项通过
```

---

## 9) 遗留与建议（本批不做，转后续批）

| 项 | 说明 | 建议归批 |
|---|---|---|
| 3. 项缺真实浏览器验证 | 仅静态断言，未跑点击 | 加 Playwright 冒烟（B-web.md §9.2） |
| `/api/health` 的 DB 探测开销 | 每次 `SELECT 1` 会新开连接（`PRAGMA journal_mode=WAL` 有开销），60s 一次可接受 | 观察即可，无需改 |
| `_max_page` 未对 `page_count` 做可信度检查 | 元数据里的页数本身可能是错的（比真实页数大），此时 clamp 上限偏松，但仍由前端 `numPages` 兜底 | 维持现状 |
| `?v=` 手工维护 | 本次已顺手改对，但机制仍易漏 | 批 N：自动计算资源版本（B-web.md §8.2） |
| `tests/test_regressions.py:82` 断言过期 | 见 §7，需 adapter-reviewer 同步 | 与 `sites/na_jp.py` 同批 |
| 标注 page 未约束到该书真实页数 | 只有量级上限 | 后续功能增强 |
