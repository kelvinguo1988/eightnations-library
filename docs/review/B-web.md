# 审查报告 B：Web 后端与前端（web/app.py + templates + static + 阅读器）

> 审查人：代码审查员 B（Web 安全 + 前端质量）
> 审查日期：2026-10-27
> 审查范围：`web/app.py`(761 行)、`web/templates/*.html`(8 个)、`web/static/reader.js`(475 行)、`web/static/filters.js`、`web/static/*.css`、`web/static/pdfjs/`
> 方法：**逐行静态审查 + 本地实测**（`EIGHTNATIONS_DATA=/tmp/reviewB python3 -m uvicorn web.app:app --port 8099`，插入 1001 条测试数据，curl 验证；测试完毕已 kill 进程、未触碰仓库 `data/`、未联网、未 commit）
> 依赖版本（实测）：jinja2 3.1.6 / fastapi 0.111.0 / starlette 0.37.2 / Python 3.9

---

## 0. 先说结论（TL;DR）

| 维度 | 评分 | 一句话 |
|---|---|---|
| **XSS 防护** | **7.5 / 10** | 上一个提交 `e088b30`「全量XSS收口」**基本属实**：autoescape 实测为 `True`、`|safe` 零使用、`tojson` 会自动转义 `<`、reader.js 两个 innerHTML 拼接点都有 `escapeHtml`/`safeColor`。**没有找到可触发的存储型 XSS**（详见 §1.1）。扣分项：全链路依赖"每个模板作者都不写裸 `{{ }}` 进 script/属性"这种**约定而非机制**，无 CSP 兜底。 |
| **CSRF** | **3 / 10** | 无任何 token、无 Origin/Referer 校验、`/api/*` 接受 `text/plain`（简单请求，无需预检）、`DELETE` 连 body 都不需要。**但**项目无 Cookie 会话 → 攻击者拿不到数据、也拿不到"用户身份"，危害上限是"把人家的库状态改乱"。**务实结论：P2，不要引入 CSRF token，用 5 行 Origin 校验 + SameSite 兜底即可**（§1.2）。 |
| **鉴权/越权** | **1 / 10** | 全站零鉴权，这是**设计选择**（README 明写"局域网"），但 **DESIGN.md 只写了一句"仅局域网访问 + 可选简单 Token"，代码里没有任何一行显式化这个假设，也没有任何启动告警/绑卡限制**。任何能访问 8080 的设备 = 管理员。 |
| **API 健壮性** | **4 / 10** | **5 个接口收到非法 JSON 直接 500**（实测）；`text`/`note`/`page` 无上限（实测 10MB 文本入库成功）；`/read?p=` 完全不校验；PATCH/DELETE 不校验对象存在性与归属。 |
| **阅读器正确性** | **6 / 10** | 竞态防护写得比预期好（`renderTasks.get(p) !== entry` 三处校验、`job.cancel()`、`io.disconnect()`）。但有 **1 个确定性功能 bug**（`data-layout` 永远不被 JS 写入 → 布局 CSS 全不生效）、**1 个主题被静默吞掉**（`sepia` 不在后端白名单）、无 resize 处理。 |
| **SEO/可访问性** | **2.5 / 10** | **全站 0 个 `aria-*` 属性**；阅读器 9 个图标按钮里 5 个"可访问名为空"；`alt=""` 三处；`.mut` 对比度 3.97:1 未达 WCAG AA；无 skip-link、无 focus 样式、无 `role="dialog"`、`user-scalable=no`。 |
| **信息架构/UX** | **6 / 10** | 5 个页面的信息层级清楚、空状态齐全（`{% else %}` 都有）。扣分：书架"已归档"tab 一次渲染 1000 册（实测 317KB HTML）、settings 有**嵌套 form 导致设置保存功能实际损坏**、书签 note 是半成品、无 loading 态。 |
| **代码质量** | **5.5 / 10** | `app.py` 761 行单文件混了 HTML 路由 + JSON API + 后台线程 + 上传/导入，应该拆；library/review 两模板筛选区 ~90% 重复；无类型标注；`from core.pipeline import _dest_dir` 直接 import 私有函数。 |
| **综合** | **5.1 / 10** | **XSS 这块可以放心了，真正的风险在其它地方：SSRF + 无鉴权 + 输入无上限 + 500 泛滥。** |

**P0 = 0 条，P1 = 10 条，P2 = 31 条**（完整索引见附录 B）。

> 为什么 P0 是 0 条：本项目的威胁模型是"局域网 + 单人/家庭使用 + 无鉴权"。在**这个模型内**，没有任何一条漏洞能导致远程代码执行、数据外泄给第三方、或绕过"本该有"的权限边界（因为边界本来就不存在）。SSRF 是唯一可能"打穿到 NAS 内网其它服务"的一条，但它需要先在设置页提交表单，见 §1.3 的等级论证。如果**未来**要暴露到公网或多人共用，SSRF 与无鉴权立刻升为 P0。

---

## 1. 安全

### 1.1 XSS —— 验证"全量收口"是否真实

上一提交 `e088b30 fix(web): 全量XSS收口...` 的自述需要逐条核实。**实测结果：基本属实。**

#### 1.1.1 正面证据（逐项实测，可以放心）

```text
$ python3 -c "from web import app as A; print(A.templates.env.autoescape)"
autoescape: True                     ← Jinja2Templates(directory=...) 对 .html 默认开启

$ grep -rn "|safe" web/templates/
（无输出）                            ← 零处 |safe

$ tojson 是否转义 <
{{ v|tojson }} with v='<script>alert(1)</script>'
→ "\u003cscript\u003ealert(1)\u003c/script\u003e"     ← 已转义，data-anns 安全

$ 属性上下文转义
<i data-x="{{ v }}"> with v='" onmouseover=alert(1) x="
→ <i data-x="&#34; onmouseover=alert(1) x=&#34;">      ← 引号被转义，不逃逸属性
```

关键的两个"隐形坑"也都**没有踩**：

- **`jp2t` 过滤器**（`core/text.py:45`）返回的是 `str` 而不是 `Markup`，实测 `hasattr(r,'__html__')` 为 `False` → `{{ title|jp2t }}` 仍会被转义。**如果哪天有人为了性能把 `jp2t` 改成返回 `Markup`，全站题名立刻变成存储型 XSS 面。** 建议在 `jp2t` 上加一行注释警告（§7 建议）。
- **`data-invert='{{ ...|tojson|forceescape }}'`**（`library.html:33`、`review.html:29`）用了 `tojson|forceescape` 双保险，实测渲染后 `JSON.parse(el.dataset.invert)` 能正确还原 → **`8ff6a8f` 修的"反选按钮引号嵌套失效"确实修好了**。

#### 1.1.2 reader.js 的 `innerHTML` 拼接点（共 2 处，逐处核查）

| 位置 | 拼进 innerHTML 的东西 | 转义情况 | 判定 |
|---|---|---|---|
| `reader.js:242-247` `refreshBookmarks()` | `b.page`、`b.note` | `pg = +b.page`（数值化，`NaN` 也不会注入）、`escapeHtml(b.note)` | ✅ 安全 |
| `reader.js:298-313` `openEditor()` | `a.kind`（KIND_ICON/KINDS 查表）、`+a.page`、`a.text` | 查表命中固定值、`+a.page \|\| 0`、`escapeHtml(a.text)` | ✅ 安全 |

`renderOverlays()`（`reader.js:278-280`）是唯一"后端数据直接进 CSS"的地方，用了 `safeColor()`（`/^#[0-9a-fA-F]{6}$/`）＋**服务端** `COLOR_RE`（`app.py:113`）双保险 → ✅ 安全。

#### 1.1.3 但仍存在 3 个"靠约定维持"的脆弱点

| 级别 | 位置 | 问题 |
|---|---|---|
| **P2** | `web/templates/detail.html:31` | `{{ meta.files[0].sha256[:16] }}…` —— `meta.json` 由**下载管线**写入。若 `files` 为空列表则 `IndexError` → 500；若 `files[0]` 不是 dict 则 `TypeError` → 500。**目前无害（只影响本页），但它暴露了一个模式：任意 `.html` 里都在信任磁盘 JSON 的结构。** 改成 `{{ (meta.get('files') or [{}])[0].get('sha256', '?')[:16] }}` |
| **P2** | `web/templates/settings.html:15` | `<pre>{{ storage.migrate_cmd }}</pre>`，内容来自 `core/mounts.py` 的 `docker` 命令字符串。会被自动转义 ✅，但**这段文本本身会直接粘贴到 NAS SSH 里执行**，且其中 `curl -O https://raw.githubusercontent.com/.../migrate-nas.sh && sh migrate-nas.sh` 是**从 main 分支拉脚本无校验直接执行**。这是整个应用里社会工程学风险最高的一行文本。建议改为 `curl -O .../migrate-nas.sh@<tag>` + 打印 hash 让用户核对。 |
| **P2** | `web/templates/*.html`（全局） | **没有 CSP**。目前 XSS 面为空，加 CSP 的意义不是"修漏洞"而是"上保险"——将来任何一次模板改动写错，CSP 能兜住。可行性见 §1.6。 |

**结论：XSS 维度可以判定为"已收口"，不是 P1。** 但请把这份报告的 §1.1.1 三条实测作为回归基线写进 tests（见 §6.4）。

---

### 1.2 CSRF —— 评估真实风险等级（结论：**P2，不要过度设计**）

#### 实测证据

```text
# 1) 无任何 token / 无 Origin 校验
POST /api/links   with Origin: http://evil.example  → 200   ← 未拒绝
POST /api/review  with Origin: http://evil.example  → 200   ← 未拒绝

# 2) JSON 接口接受 text/plain（= 跨站"简单请求"，浏览器不发预检）
POST /api/review  Content-Type: text/plain, body {"action":"ignore","ids":[1]}  → 200

# 3) DELETE 连 Content-Type / body 都不需要
DELETE /api/annotations/1  （无 body、无 CT）  → 200

# 4) 全站无 Cookie（实测 POST /api/links 无 cookie 也返回 200；cookie jar 为空）
# 5) 无 CORS 中间件（跨站读不到响应，但"写"已经发生）
```

#### 影响分析（这是关键：别把等级报高）

一个恶意页面（用户在同一局域网内点开一个链接）能通过 `<form action="http://nas:8080/settings" method=post>` 或 `<script>fetch('http://nas:8080/api/review',{method:'POST',mode:'no-cors',body:'{"action":"ignore","ids":[1,2,3]}'})</script>` 做到：

- 把设置页的 `enabled_*` / `quota_*` / `catalog_url_*` 全部改成攻击者想要的值（**这是 SSRF 的前置利用链，见 §1.3**）
- 把用户勾选待审核的书批量标为 `ignored`
- 删除标注/书签（需要猜 id，递增整数，很容易）
- 触发 `/api/links`（幂等，无害）

做不到：
- **读不到任何数据**（无 CORS → 跨站 fetch 读不到 body；无 Cookie → 没有可窃取的会话）
- 冒充不了"另一个用户"（根本没有用户概念）
- 改不了 `data/books/` 里的 PDF 文件

> **所以本项目的 CSRF 本质是"CSRF 形态的 CSRF-free 应用的混淆"**：因为没有会话，攻击者从 CSRF 中得不到任何"权限放大"，只能造成"状态破坏"。对一个单用户 NAS 个人图书馆，这是**可接受的残余风险**。

#### 建议（务实版，工作量 ≈ 15 行）

**不要**引入 django-style CSRF token（那需要 session + 表单模板全改 + fetch 全改，收益与成本不成比例）。

推荐在 `app.py` 加一个极轻量的中间件（**顺带解决 §1.2 之外的另一个问题：Host 头未校验**）：

```python
# web/app.py —— 放在 app = FastAPI(...) 之后
from urllib.parse import urlparse

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ALLOWED_HOSTS = {h.strip() for h in os.environ.get(
    "EIGHTNATIONS_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h.strip()}

@app.middleware("http")
async def guard_writes(request: Request, call_next):
    """① 写操作校验 Origin/Referer 同源（浏览器一定带，curl 不带 → 放行本机脚本）
       ② Host 头白名单：防 DNS rebinding 让攻击页把自己当作同源"""
    if request.method in _WRITE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            o = urlparse(origin)
            # 同源判定：Host 头与 Origin 的 host 一致即可（端口一并比较）
            if f"{o.hostname}:{o.port or {'http': 80, 'https': 443}[o.scheme]}" \
               != request.headers.get("host", "").replace(":80", ":", 1).rstrip(":"):
                return JSONResponse({"error": "跨站请求被拒绝"}, status_code=403)
    host = (request.headers.get("host") or "").split(":")[0]
    if _ALLOWED_HOSTS and host not in _ALLOWED_HOSTS and not host.startswith("192.168.") \
            and not host.startswith("10.") and not host.startswith("172."):
        return JSONResponse({"error": "Host 不被允许"}, status_code=400)
    return await call_next(request)
```

> ⚠️ 上面 Host 白名单的"私网前缀放行"只是为了 NAS 场景能用，**不能**当作安全边界（`10.evil.com` 也满足前缀判断，得改成解析 IP 后判 `ipaddress.ip_address(host).is_private`）。真正的推荐做法是：**默认只允许 `EIGHTNATIONS_ALLOWED_HOSTS`，NAS 部署时把 `NAS_IP:8080` 写进 compose 的环境变量**。

**加分项**：`/api/*` 里凡是"接受 JSON 的写接口"，统一加 `Content-Type: application/json` 强制校验（`text/plain` 直接 415），这一条就能挡掉**所有** `<form>` 型 CSRF：

```python
def _need_json(request: Request) -> None:
    ct = (request.headers.get("content-type") or "").split(";")[0].strip()
    if ct != "application/json":
        raise HTTPException(415, "需要 application/json")
```

---

### 1.3 SSRF —— `catalog_url` 任意 URL，**实测可利用**

#### 级别：**P1**（含等级论证）

#### 证据链（代码 + 实测）

```text
web/templates/settings.html:56   <input type="text" name="catalog_url_{{ s['id'] }}" ...>
web/app.py:641-643               catalog_url = form.get(...).strip()
                                 if catalog_url and not catalog_url.startswith(("http://","https://")): catalog_url = ""
                                 ↑ 只校验协议，不校验主机
scheduler.py:169-172             if s["meta_strategy"] == "direct" and s["catalog_url"] and _stale(...):
scheduler.py:88                  stats = harvest(s["catalog_url"], known_uids=known, ...)
sites/na_jp.py:170               url = catalog_url if (max_pages == 1 or not fonds_id) else f"{BASE}/fonds/{fonds_id}?page={page}"
sites/na_jp.py:176               html = self.http.get(url)              ← 直接请求！无主机白名单
core/http.py:138                 r = self.session.get(url, timeout=self.timeout, headers=headers)   ← requests 默认 follow redirects
```

实测（本地 8099）：

```text
$ curl -X POST http://127.0.0.1:8099/settings \
    --data-urlencode "enabled_na_jp=on" --data-urlencode "quota_na_jp=10" \
    --data-urlencode "quality_na_jp=auto" --data-urlencode "catalog_url_na_jp=http://169.254.169.254/"
→ HTTP 303

$ 查库
{'id': 'na_jp', 'enabled': 1, 'catalog_url': 'http://169.254.169.254/'}
         ↑ 云元数据地址被完整接受
```

**注意 `na_jp.py:170` 的一个细节**：因为 `re.search(r"/fonds/(\d+)", catalog_url)` 匹配不到 fonds id → `fonds_id = ""` → 走 `url = catalog_url`，**攻击者给的 URL 被原样请求**，一个字节都不改。

#### 实际能做什么

1. **内网端口/主机探测**：`_stale(last_catalog_at)` 决定触发频率（默认每周巡检 + 空库首轮），scheduler 每 900s 心跳检查一次，每次 `max_pages=15` 页 + `budget=40` 详情 = **单次最多 55 个请求**打向任意内网主机任意端口。
2. **命中内网无鉴权服务并改变其状态**：`HttpClient.get` 只做 `GET`，所以能打的是"GET 就能触发副作用"的接口（很多路由器/IoT 管理页、`/cgi-bin/...` 都是）。
3. **把响应内容落进库**：`on_meta(meta)` 会把解析出的题名/uid 写进 `books` 表 → 攻击者可以在"新书审核"页看到内网服务的响应片段（**盲 SSRF 变有回显 SSRF**）。这是危害最大的一点。
4. **重定向绕过**：`requests` 默认 `allow_redirects=True`，即使将来加了主机白名单，一个 `http://allowed-host/redirect?to=http://169.254.169.254/` 仍能绕过 → **必须显式关掉重定向或逐跳校验**。

#### 等级论证：为什么不是 P0

利用它需要 **跨站 POST /settings（§1.2）或本机访问**。目前 8080 暴露在局域网，所以"局域网内任一设备"即可完成整条链。**它无法导致 RCE、无法读文件、无法绕过鉴权（没有鉴权）**，危害集中在"把 NAS 当跳板探测内网 + 污染书库"。放 P1 合适；**若将来公网暴露，直接升 P0。**

#### 建议修法（最小改动，~10 行）

```python
# web/app.py —— 新增
_ALLOWED_CATALOG_HOSTS = {
    "gallica.bnf.fr", "www.loc.gov", "dl.ndl.go.jp", "www.digital.archives.go.jp",
}  # 与 sites/*.py 里 BASE 常量保持一致；也可从 sites 模块统一导出

def _valid_catalog_url(u: str) -> str:
    """catalog_url 白名单：仅允许已知馆藏域名 + 禁止私网/元数据地址。
    返回空串表示拒绝。"""
    from urllib.parse import urlparse
    import ipaddress
    try:
        p = urlparse(u)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    try:                                   # 直接写 IP 的一律拒绝（含 169.254/127/10/192.168/172.16）
        ip = ipaddress.ip_address(p.hostname)
        return ""
    except ValueError:
        pass
    if p.hostname.endswith(".local") or p.hostname in ("localhost",):
        return ""
    if p.hostname not in _ALLOWED_CATALOG_HOSTS:
        return ""
    return u

# settings_save() 里
catalog_url = (form.get(f"catalog_url_{sid}") or "").strip()
catalog_url = _valid_catalog_url(catalog_url)
```

**同时**在 `core/http.py` 的 `HttpClient.get` 加一道**兜底**（防"白名单域名被 DNS 指向内网"这种 rebinding）：

```python
# core/http.py —— HttpClient.get / download 共用
import ipaddress, socket
from urllib.parse import urlparse

def _resolve_is_public(host: str) -> bool:
    try:
        for *_ , (fam, _, _, _, sa) in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except OSError:
        return False
    return True

# get() 里，发请求前：
if not _resolve_is_public(urlparse(url).hostname or ""):
    return None
r = self.session.get(url, timeout=self.timeout, headers=headers, allow_redirects=False)
```

> `allow_redirects=False` 是很关键的一行：现有代码依赖重定向（`sites/bnf.py` 的 SRU 应该不需要），改之前先确认没有站点依赖 302。若确实需要，则实现"最多 3 跳、每跳校验 host"的循环。

---

### 1.4 无鉴权模型 —— 假设未被显式化（**P1**）

#### 证据

```text
$ grep -rn "鉴权\|局域网\|auth" README.md docs/DESIGN.md web/app.py Dockerfile docker-compose.yml
README.md:4    审核、局域网 Web 前端浏览与在线阅读。        ← 只出现在功能描述里
docs/DESIGN.md:198  仅局域网访问 + 可选简单 Token；不引入用户系统。   ← 唯一一处"设计声明"，且在附录
web/app.py     （无命中）
Dockerfile     （无命中）
docker-compose.yml  （无命中，且 ports: ["8080:8080"] 绑 0.0.0.0）
entrypoint.sh:61    exec python3 -m uvicorn web.app:app --host 0.0.0.0 --port 8080   ← 绑所有网卡
```

**实测**：无 cookie、无 token，`GET /settings`、`POST /api/links`、`GET /api/doctor` 全部 200。

#### 问题

1. `0.0.0.0:8080` + `ports: "8080:8080"` 意味着 **NAS 的所有网络接口**（含可能存在的 WAN 侧 / 反向代理）都能访问。QNAP 默认有 Web 管理端，同机部署时很容易被顺手反代出去。
2. 代码里**没有任何一行**表达"我没有鉴权"。所有写接口（`/api/links`、`/settings`、`/upload-snapshot`、`/api/fetch`）都假定调用者可信任。
3. 没有启动告警。运维者不会在任何地方看到"你正在运行一个无鉴权的、能改写数据的管理面"。

#### 建议（低成本，按优先级）

**① 启动时打印醒目告警 + 版本号**（`app.py` 模块底部，3 行）：

```python
if os.environ.get("EIGHTNATIONS_QUIET") != "1":
    print(f"[web] ⚠️  无鉴权模式：任何能访问 {os.environ.get('EIGHTNATIONS_BIND','0.0.0.0:8080')} "
          f"的设备都可读写全部数据。请勿暴露到公网。", file=sys.stderr)
```

**② Docker 侧把安全假设写进 compose**（一行注释 + 可选绑定地址）：

```yaml
    ports:
      # 只绑 NAS 的局域网网卡，别用 0.0.0.0（QNAP 多网卡时尤其重要）
      - "${EIGHTNATIONS_BIND_ADDR:-192.168.1.10}:8080:8080"
```

**③ 全局只读开关**（强烈推荐，5 行，覆盖"给别人看看"的场景）：

```python
READ_ONLY = os.environ.get("EIGHTNATIONS_READ_ONLY") == "1"

@app.middleware("http")
async def readonly_guard(request: Request, call_next):
    if READ_ONLY and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        return JSONResponse({"error": "只读模式"}, status_code=403)
    return await call_next(request)
```

**④ 若要"可选简单 Token"（DESIGN.md 承诺过的）**：别自己写，用 `X-Admin-Token` 头 + 一次性 `?token=` 换 Cookie 的 20 行实现即可；**不要**追求完整 session 体系。

---

### 1.5 上传接口 `/upload-snapshot` —— 比预期稳健，但有 3 个坑

#### 已做对的部分（值得肯定）

```text
web/app.py:677  if source not in known: 400           ← source 白名单，防目录穿越
web/app.py:691  name = os.path.basename((f.filename or "").replace("\\", "/"))   ← 防 Windows 反斜杠穿越
web/app.py:657  _copy_capped() 流式限额（单文件 64MB / 总 256MB）  ← 不整文件入内存
web/app.py:682  uuid 目录名                                 ← 防并发互相覆盖
```

实测路径穿越**全部被拦截**：

```text
filename=../../evil.json  → 落盘为 snapshots/loc/upload_.../evil.json   ✅ 未逃出
filename=..json           → 落盘为 .../..json                          ⚠️ 见下
```

#### 坑 1：`..json` / `.json` 这类隐藏文件名（**P2**）

```python
web/app.py:692   if not name.endswith(".json") or name == ".json": continue
```

`name == "..json"` 通过了校验（只挡了 `.json`），落成 `..json`。`import_snapshot_files()` 只认 `collection_*.json`，所以**不会**被导入，但会**永久留在磁盘上**（每次上传都建新 uuid 目录，从不清理）。建议：

```python
if not name.endswith(".json") or name.startswith(".") or len(name) <= 5:
    continue
```

#### 坑 2：上传目录永不清理 → 磁盘慢速泄漏（**P2**）

```text
$ find /tmp/reviewB/snapshots -type f
.../loc/upload_2026-10-27T134652Z_dae86b47/collection_a.json
.../loc/upload_2026-10-27T134652Z_c3ce8cb2/evil.json          ← 3 次测试 = 3 个目录
```

每次上传建一个 `upload_<ts>_<uuid8>` 目录，**导入成功后不删除**、失败也只删当前目录。反复上传 = 无限堆积（上限 256MB/次，无总配额）。建议导入成功后 `shutil.rmtree(ts_dir, ignore_errors=True)`（导入是同步读完即入库，删掉不影响），或至少保留最近 N 次。

#### 坑 3：zip 炸弹不存在，但 **JSON 炸弹存在**（**P2**）

`accept=".json"` + 不校验内容是 JSON → 传一个 64MB 的**深层嵌套** JSON（`[[[[...]]]]` 或 `{"a":{"a":{...}}}`）：

- `json.load()` 在 CPython 3.9 上对深层嵌套会抛 `RecursionError` → 被 `importer.py:79` 的 `except Exception: continue` 吞掉 → **静默跳过，用户看到"导入 0 本"却没有任何错误提示**。
- 更实际的：`payload["results"]` 有 100 万条 → `parse_snapshot` 循环 + 每条一次 `upsert_book`（一次事务）→ **上传请求会把 worker 线程占住几分钟**，且 `import_snapshot_files` 是**同步**调用（`web/app.py:715`，`async def` 里直接调）→ **整个事件循环卡住，全站无响应**。

这是本接口最实质的问题，见 §2.1（同类问题：`async def` 里跑同步重活）。

#### 坑 4：无鉴权上传 + 无总配额（**P1，与 §1.4 同源**）

`/upload-snapshot` 会写 `data/snapshots/` 并触发**全库 upsert**。任何局域网设备可以反复灌假书目把"新书审核"页刷爆、把 `library.db` 撑大。这没有独立的修法，取决于 §1.4（只读开关 / Token）。

---

### 1.6 路径穿越 / 静态挂载 —— **实测安全**（P2 观察项）

```text
GET /data/             → 404   （Starlette StaticFiles 不列目录）✅
GET /data/../db/library.db  → 404  ✅
GET /static/../core/db.py   → 404  ✅
GET /openapi.json      → 200   ⚠️ 见下
GET /docs              → 404   （已 docs_url=None）
```

`book_urls()`（`app.py:61-80`）已用 `_dest_dir` 做 `commonpath` 校验，`rel` 再拼进 `/data/{rel}/{n}`；`n` 来自 `os.listdir` + 正则 `re.fullmatch(r"book_\d+\.pdf", n)` → **无穿越面**。

**P2 观察项：`/openapi.json` 未关闭。** `app.py:37` 只关了 `docs_url`/`redoc_url`，FastAPI 仍会生成 `/openapi.json`，把**全部路由、参数名、模型**暴露出去（对攻击者做侦察很有用，包括 `/upload-snapshot` 的字段名）。修复 1 行：

```python
app = FastAPI(title="八国联军图书馆", docs_url=None, redoc_url=None, openapi_url=None)
```

**P2 观察项：`/data` 挂载把 `data/books/` 整棵树暴露为可下载。** 包括 `meta.json`（含 sha256、来源 URL）和书名硬链接。这是**有意的功能**（`detail.html:27` 的下载按钮），只需知道：知道 `source_id/collection/uid` 的人可以不经过任何页面逻辑直接拉 PDF。考虑给 PDF 下载加 `Content-Disposition: inline` 之外的访问记录（可选）。

---

### 1.7 SQL 注入 / LIKE 通配 —— **无注入，但有 2 个语义 bug**（P2）

`core/db.py:_book_filters()` 全程参数化，`era IN ({','.join('?'*len(vals))})`、`subjects LIKE ?` 都是占位符，**无拼接注入**。`ORDER BY id` 是硬编码，无注入。

但有两处**行为**问题：

```python
# core/db.py:405
sql += " AND (title LIKE ? OR alt_title LIKE ? OR shelf_id LIKE ? OR subjects LIKE ?)"
args += [f"%{keyword}%"] * 4          # ← keyword 里的 % 和 _ 未被转义
```

- **Bug 1（P2）**：搜索 `%` 会命中全库（`LIKE '%%%'`）；搜索 `_` 会匹配任意单字符。用户搜 `民国_年` 会得到意外结果。修法：
  ```python
  kw = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
  sql += " AND (title LIKE ? ESCAPE '\\' OR ...)"
  args += [f"%{kw}%"] * 4
  ```
- **Bug 2（P2）**：`subjects LIKE '%"tag"%'` 依赖 JSON 字符串里的引号，配合上面未转义问题，`subjects=白,` 这类输入会退化成宽匹配。影响仅限筛选结果不准。

---

### 1.8 信息泄露（P2）

| 位置 | 泄露内容 | 评估 |
|---|---|---|
| `app.py:608` `storage.data_dir` → `settings.html:8` | 容器内路径 `/data` | 低危（本来就写在文档里） |
| `app.py:610-613` `mount_source` | **宿主机真实路径**（如 `/share/Container/eightnations/data` 或 `/Container/container-station-data/lib/docker/volumes/<hash>/_data`） | 低危，但有帮助侦察（告知攻击者 NAS 目录结构） |
| `app.py:613-615` `host_hint` | `EIGHTNATIONS_HOST_DATA` 环境变量原样回显 | 低危 |
| `app.py:730-735` `/api/doctor` | `run_checks()` 全量结果（含路径、sha256、缺失文件清单） | **中低危**：这是"内部运维态"直接对局域网开放。建议在 `api_doctor` 里加 `?verbose=1` 才返回 `checks` 明细，默认只返回 `{ok, issues}` |
| `app.py:747-749` `/api/health` | `db: exists bool` | 无害，但**它不反映"db 能否打开"**，做容器 healthcheck 会误报 OK（这是 D 审查的范围，这里提示一句） |
| `app.py:752-761` `/api/stats` | 各状态书数、总字节 | 低危 |

**整体判断**：这些都在"局域网可信"的威胁模型内可以接受，**但都应当由 §1.4 的鉴权/只读开关统一收口**，而不是逐个打补丁。

---

## 2. HTTP / API 设计

### 2.1 `await request.json()` 无 try → **5 个接口 500**（P1）

#### 实测

```text
POST /api/bookmarks      非法JSON -> 500
POST /api/annotations    非法JSON -> 500
POST /api/reading-tick   非法JSON -> 500
POST /api/review         非法JSON -> 500
POST /api/retry          非法JSON -> 500
POST /api/review         空 body   -> 500
POST /api/review         无 CT     -> 500
```

服务端堆栈确认：

```text
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
INFO: 127.0.0.1:57646 - "POST /api/annotations HTTP/1.1" 500 Internal Server Error
```

#### 位置（5 处）

- `web/app.py:346` `api_bookmark_add`
- `web/app.py:372` `api_annotation_add`
- `web/app.py:393` `api_annotation_edit`
- `web/app.py:408` `api_reading_tick`
- `web/app.py:479` `api_review`、`web/app.py:524` `api_retry`

（`api_progress` 是唯一"侥幸"没 500 的：它先查 `get_book` 返回 404 提前 return 了 —— 顺序依赖，不算修好。）

#### 影响

500 会打印整段 traceback 到日志（日志膨胀），前端收到的是 HTML 错误页而不是 JSON，导致 `await r.json()` 在前端抛 `SyntaxError`（见 §3.5）。对局域网个人项目不是安全事件，但**是"服务不健壮"的明确信号**，且掩盖真实 bug。

#### 修法：抽一个公共帮助函数，5 处各改 1 行

```python
# web/app.py —— 顶部
from json import JSONDecodeError

async def _json_body(request: Request) -> dict:
    """统一的 JSON body 读取：非法/空 body → 400 而非 500。"""
    try:
        body = await request.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    return body

# 用法
async def api_bookmark_add(request: Request):
    body = await _json_body(request)
    ...
```

（`api_fetch` 用的是 FastAPI 的 `payload: dict` 参数，空 body 返回 **422** 而不是 400 —— 语义不一致，但可接受，P2。）

---

### 2.2 输入无上限（P1）

实测：

```text
POST /api/annotations text = 10MB           -> 200   ← 10MB 文本一次写进 SQLite
POST /api/annotations page = 2**62          -> 200   ← 天量整数入库
POST /api/bookmarks   note = 1MB            -> 200
GET  /read/1?p=99999999                     -> 200，且渲染进 data-page="99999999" 和 <input value="99999999">
```

#### 逐项

| 字段 | 位置 | 现状 | 建议 |
|---|---|---|---|
| `annotation.text` | `app.py:386` `str(body.get("text") or "")` | **无任何上限**（只有 bookmarks 的 note 截了 300） | `[:2000]` |
| `annotation.page` | `app.py:374` `int(body["page"])` | 无范围 | `min(max(page,1), page_count+50)` |
| `annotation.*_coords` | `app.py:375` | 已 clamp 到 [0,1] ✅ | — |
| `bookmark.page` | `app.py:348` | 无范围 | 同上 |
| `bookmark.note` | `app.py:353` | `[:300]` ✅ | — |
| `progress.page` | `app.py:324` | `max(1, ...)` 只有下界，**无上界** | 与 `page_count` 对齐 |
| `progress.zoom` | `app.py:326` | `[0.4, 3]` ✅ | — |
| `review.ids` | `app.py:482` | `body.get("ids")` 可以是**任意长度**列表 → 循环 N 次 `set_status`（N 次事务） | `ids[:500]` 或分批 |
| `reading-tick.seconds` | `app.py:411` | `[0,120]` ✅ | — |
| `/explore?page=` | `app.py:231` | `max(page,1)` 无上界 → `offset` 巨大，SQLite 无所谓但 `pages` 计算异常 | `min(page, pages)` |
| `/read?p=` | `app.py:307-308` | **完全不校验**，直接 `prog_d.update(page=p)` | 见下 |

#### `/read?p=` 的具体修法（app.py:307）

```python
    if p:                       # URL 指定页码优先（书签/检索跳转）
        # 页码必须落在 [1, total_pages]，否则会污染阅读进度并渲染出非法 data-page
        hi = total_pages or 1
        prog_d.update(page=min(max(p, 1), hi), scroll_pct=0)
```

> 注意 `total_pages` 在 `app.py:297-304` 可能为 `0`（meta.json 缺失），此时 `hi=1`，`p` 一律归 1 —— 比"渲染 99999999"好得多。真正的页码上限最终由前端 `pageCount` 决定。

---

### 2.3 IDOR / 未校验对象存在性（P1）

```text
PATCH  /api/annotations/999999   → 200   （标注不存在也返回成功）
DELETE /api/annotations/999999   → 200
DELETE /api/bookmarks/1/999999   → 200
```

对应代码 `app.py:390-402`：`api_annotation_edit` / `api_annotation_del` / `api_bookmark_del` **只执行 SQL，不检查 rowcount，不检查 book_id 归属**。

在"无用户系统"的前提下这不算越权（没有别人的数据），但：

1. **前端无法区分"成功"与"对象已不存在"** → 用户删了一个已被并发删掉的标注，界面显示"已删除"，数据却没变。
2. **`api_annotation_edit` 更糟**：它把任意 `ann_id` 的 kind/color/text 覆盖掉，**完全不管这个标注属于哪本书**。虽然前端只传自己页面的 id，但一旦有 bug 传错，就会静默改坏别的书的标注。

修法（3 处，各 2 行）：

```python
@app.patch("/api/annotations/{ann_id}")
async def api_annotation_edit(ann_id: int, request: Request):
    d = get_db()
    body = await _json_body(request)
    if not d.get_annotation(ann_id):          # 需要一个 get_annotation（或 UPDATE ... RETURNING）
        return JSONResponse({"error": "标注不存在"}, status_code=404)
    kind, color = _ann_fields(body)
    d.update_annotation(ann_id, kind, color, str(body.get("text") or "")[:2000])
    return {"ok": True}
```

若不想加 `get_annotation`，最简单：让 `update_annotation`/`delete_annotation`/`delete_bookmark` 返回 `cur.rowcount`，`if not n: return 404`。

---

### 2.4 其它 API 设计问题（P2）

| 位置 | 问题 | 建议 |
|---|---|---|
| `app.py:255`、`292`、`295`、`423` | 404 返回**裸文本** `HTMLResponse("未找到该书")`，无导航、无样式，且 `/read` 的 404 文案"该书 PDF 尚未下载"返回的是 **404 而非 200/409**（语义 OK，但没有回首页链接） | 做一个 `templates/error.html`，或至少返回 `<a href="/">返回</a>` |
| `app.py:251` `book_id: int` | `/books/abc` → **422**（FastAPI 默认），暴露实现细节 | 可接受；若要友好，改 `book_id: str` 手动解析 |
| `app.py:317` `api_progress` | 先 `get_book` 查一次再写，**每 1.2s 一次滚动都会打一次 SELECT** | 靠 §6 的性能建议统一优化；短期可跳过存在性检查（FK 会兜） |
| `app.py:337` `api_bookmarks(book_id: int)` | 无 `book_id` 校验，`?book_id=-1` 返回 `[]` ✅ | — |
| `app.py:405` `api_reading_tick` | 没校验 book 存在 → 依赖 DB 外键抛异常 → 400（被 `except` 捕获） | `_json_body` 改造时顺手加 `get_book` 检查 |
| `app.py:445` `api_favorite` | **同步 def 但只做一次 DB 写**，走线程池，OK ✅ | — |
| `app.py:537` `api_fetch` | 注释说"同步 def 走线程池防卡事件循环" —— **写得很对**，但 `upload_snapshot`（`app.py:670`）明明是 `async def` 却在里面同步跑 `import_snapshot_files`（重活） | **改成 `def`**（FastAPI 自动走线程池）。见 §2.5 |
| `app.py:496-518` `api_review` download 分支 | `threading.Thread(daemon=True)` 起后台线程跑 `fetch_one` —— **进程被 kill 时线程直接消失**，正在下载的 `.part` 靠 `reap_stuck` 租约兜底（可以） | 可接受，但建议把"已开跑"状态落库（目前 `set_status(i,"queued")` 先做，OK） |

### 2.5 `async def` 里跑同步阻塞重活（P1）

```python
# web/app.py:625-628
@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()      # 表单可能很大
    ...
    with d.connect() as conn:        # 同步 SQLite 写
        conn.execute("UPDATE sources ...")
```

```python
# web/app.py:670-721
@app.post("/upload-snapshot")
async def upload_snapshot(...):
    ...
    new, updated = import_snapshot_files(d, source, ts_dir)   # ← 同步：解析 JSON + N 次 upsert + 每次一个事务
```

`import_snapshot_files` 对 2000 个文件的快照会做 **2000 次文件读取 + 每次 `upsert_book` 一个隐式事务**。在此期间**整个 uvicorn 事件循环被独占**，所有页面（包括正在阅读的 PDF 分页请求）全部停顿。

`api_review` 的 `approve` 分支（`app.py:494-495`）在 `async def` 里循环 `d.set_status(i, "queued")`，同样问题（批量勾选 500 本 = 500 次事务）。

**修法**：把这些改成普通 `def`（FastAPI 会丢进线程池），或者把批量写包进**单个事务**：

```python
# 批量 set_status 合并为一个事务
with d.connect() as conn:
    conn.executemany("UPDATE books SET status=?, decided_at=? WHERE id=?",
                     [("queued", utcnow(), i) for i in ids])
```

（`d.connect()` 的 `__exit__` 是否 commit 需确认 `core/db.py` 实现 —— 若已 commit 就直接用，效率差 500 倍。）

---

## 3. 前端（reader.js）正确性

> reader.js 整体质量**高于预期**：`renderTasks` 用了 `{rendering, canvas, job}` 三字段 + **3 处** `renderTasks.get(p) !== entry` 身份校验 + `job.cancel()`，`IntersectionObserver` 有 `io.disconnect()` 清理，`exportSaved` 节流。**竞态和内存泄漏这块基本没问题。** 以下是真问题。

### 3.1 `data-layout` 永远不被写入 → 布局切换的 **CSS 完全失效**（P1，确定性 bug）

#### 证据

```text
$ grep -n "dataset.layout" web/static/reader.js
（无输出）                                  ← JS 从不写 root.dataset.layout

$ grep -n "data-layout" web/templates/read.html
17:     data-layout="{{ prog.get('layout', 'scroll') }}"

$ grep -n "data-layout" web/static/reader.css
26: #reader[data-layout=single] ...
27: #reader[data-layout=single] ...
28: #reader[data-layout=single] ...
39: #reader[data-layout=double] ...
40: #reader[data-layout=double] ...
```

#### 推演

- 首次访问 `/read/1`：`prog` 为空 → `data-layout="scroll"`（模板默认值）→ 用户点"布局" → `setLayout("double")` → `layout` 变量变了、按钮图标变了、`rerenderAll()` 跑了，**但 `root.dataset.layout` 仍是 `"scroll"`** → `#reader[data-layout=double]` 的 grid 布局**不生效**。
- 于是"双页"实际上**只是页面宽度变一半的两列 flex 堆叠**（因为 `#pages` 没有 grid），"单页"的 `position:absolute; inset:0` 也不生效 → **单页模式实际表现是"连滚但只渲染当前页"**。
- 只有**刷新页面**后（`/api/progress` 已存 `layout=double` → 模板渲染 `data-layout="double"`）才正确。用户感受就是"切布局要点两次/得刷新"。

#### 修法（1 行，`setLayout` 里补上）

```javascript
function setLayout(l) {
  layout = l;
  root.dataset.layout = l;          // ← 补这一行，让 #reader[data-layout=...] 的 CSS 生效
  $("layout-btn").textContent = { scroll: "⇔", double: "⧉", single: "📄" }[l];
  rerenderAll(); saveProgressSoon();
}
```

同时在 `init()` 里也同步一次（覆盖 `prog.layout` 非法被 JS 归一的情况）：

```javascript
  root.dataset.layout = layout;      // 放在 buildPlaceholders() 之前
```

### 3.2 `sepia` 主题被后端白名单静默吞掉（P1，实测确认）

```text
app.py:115        THEMES = {"light", "night"}                    ← 少了 sepia
app.py:333        theme if theme in THEMES else "light"          ← 静默降级
reader.js:438     theme = { light:"sepia", sepia:"night", night:"light" }[theme]   ← 前端循环 3 个主题
reader.css:34-36  #reader[data-theme=sepia] ...                  ← CSS 已经写好了
read.html:11      data-theme="{{ prog.get('theme', 'light') }}"
```

实测：

```text
$ curl -X POST /api/progress/1 -d '{"page":5,"theme":"sepia","layout":"double","zoom":1.5}'
{"ok":true}
$ 查库 → {'night': 0, 'theme': 'light', ...}      ← sepia 变成了 light
```

**影响**：用户点主题按钮循环 `light → sepia → night → light`，每 1.2s 自动保存；**sepia 永远存不住**，刷新后回到 light。用户会觉得"护眼纸色主题是坏的"。

**修法**（1 行）：

```python
THEMES = {"light", "sepia", "night"}
```

> 这类"前后端枚举不同步"值得**机制化**修复：把枚举集中在 `web/constants.py`，模板/JS 通过 `{{ ... }}` 注入，见 §6.2。

### 3.3 无 `resize` / `orientationchange` 处理（P1，移动端明显）

`reader.js` 全文无 `resize` 监听。`fitWidth()` 只在 `renderPage()` 时读取 `viewer.clientWidth`。

- 手机横竖屏切换 / 桌面拖窗口 → canvas 仍是旧尺寸 → **页面模糊（放大）或留黑边（缩小）**。
- 更糟：`#pages` 的 `style.width = (viewer.clientWidth - 16) * zoom + "px"`（`buildPlaceholders` 里）也是旧值 → 双页布局错位。

**修法**（节流 200ms，避免缩放窗口时疯狂重渲染）：

```javascript
let resizeTimer = null;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const p = layout === "single" ? curPage : detectPage();
    rerenderAll();                       // 重建占位（重算 #pages 宽度）
    goToPage(p, 0, true);
  }, 200);
}, { passive: true });
```

### 3.4 `single` 布局下 `IntersectionObserver` 与 `goToPage` 的冗余渲染（P2）

`goToPage()` 在 `single` 布局下调用 `renderPage(p); renderPage(p+1); renderPage(p-1);`，而 `buildPlaceholders()` 里**所有**占位 div 都被 `io.observe()`（`reader.js:75`）。单页模式下所有 div 是 `position:absolute; inset:0` **完全重叠**，所以 IO 会认为"全部可见"→ 每页都触发 `renderPage` → **一次翻页渲染 N 页**（大 PDF 直接卡死）。

修法：`single` 布局时不要 observe 全部占位：

```javascript
for (let i = 1; i <= pageCount; i++) {
  ...
  pagesEl.appendChild(div);
  if (layout !== "single") io.observe(div);   // 单页模式由 goToPage 显式渲染
}
```

> 同时 `renderPage()` 里的 `holder.style.aspectRatio` 在 single 布局下会被 CSS `max-height:100%` 覆盖，无实际作用，可跳过。

### 3.5 前端 `fetch` 全链路无错误处理（P2）

```javascript
reader.js:150   fetch(`/api/progress/${BOOK}`, {...})         ← 不检查结果
reader.js:170   fetch("/api/reading-tick", {...})             ← 不检查结果
reader.js:234   await (await fetch(`/api/bookmarks?book_id=${BOOK}`)).json()   ← 若后端 500 返回 HTML → .json() 抛 SyntaxError，未捕获 → 书签抽屉整块不渲染
reader.js:257   await fetch(`/api/bookmarks/...`, {method:"DELETE"})   ← 不看 ok
reader.js:335/341/348  PATCH / DELETE 标注                     ← 不看 ok，直接 toast("已保存 ✓")
reader.js:443   await (await fetch(`/api/bookmarks?book_id=...`)).json()
```

**最典型的用户可见后果**：`/api/annotations` 500（§2.1 的非法 JSON 不会，但其它异常会）→ `refreshBookmarks()` 在 `ul.innerHTML = ...` 之前抛异常 → 书签抽屉显示"..."（模板里的静态内容已被 `ul.innerHTML` 覆盖？不，是在覆盖前抛错 → 保留模板的 `{% for %}` 内容，看起来正常但**不会更新**），且 `$("bm-btn")` 星星状态不更新。

**修法**：加一个统一的 fetch 包装（10 行，同时解决"后端不是 JSON 时给出人话错误"）：

```javascript
async function api(path, opts = {}) {
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { toast("网络错误，请重试"); throw e; }
  if (!r.ok) {
    let msg = r.status;
    try { msg = (await r.json()).error || msg; } catch (e) {}
    toast("操作失败：" + msg);
    throw new Error(msg);
  }
  return r;
}
// 用法：const list = await (await api(`/api/bookmarks?book_id=${BOOK}`)).json();
//       await api(`/api/annotations/${a.id}`, {method:"DELETE"});
```

### 3.6 标注创建的两步走 + 阈值不一致（P2）

```javascript
reader.js:404   if (w < 0.02 || h < 0.01) return;              ← 前端阈值 2% / 1%
app.py:377      if x1 - x0 < 0.01 or y1 - y0 < 0.01:           ← 后端阈值 1% / 1%
```

宽度 1%~2% 的框：前端放行 → 后端 400 → 前端 `r.ok` 为 false → **静默丢弃，用户画了个高亮什么都没发生、也没有提示**。建议前后端阈值统一到常量，并在 `r.ok` 为 false 时 `toast("选区太小")`。

另外：**创建时强制 `kind:'highlight', color: COLORS[0]`，然后立刻弹编辑器**（`reader.js:405-418`）。这导致：
- 网络慢时，用户看到"框消失了 1 秒后才变成高亮"（乐观更新的缺失）
- 用户如果画完就关页面，会留下一个**空高亮**（`text:""`）
- 建议：先本地乐观渲染（临时 id），成功后替换为服务端 id，失败则回滚。

### 3.7 书签备注是半成品功能（P2）

```javascript
reader.js:446-448  await fetch("/api/bookmarks", {..., body: JSON.stringify({ book_id: BOOK, page: curPage })})
                                          ↑ 从不发 note
app.py:353         d.add_bookmark(book_id, page, str(body.get("note") or "")[:300])
reader.js:245      ${b.note ? "· " + escapeHtml(b.note) : ""}          ← UI 会渲染 note
```

后端和渲染都支持 `note`，但**前端没有任何输入框能让用户填 note**。要么补一个"添加书签时输入备注"的 prompt/内联输入，要么去掉 note 相关代码（现在是死路径，也让"导出标注 Markdown"里没有任何书签）。

### 3.8 阅读心跳的语义问题（P2）

```javascript
reader.js:168-173
function readingTick() {
  if (document.visibilityState !== "visible") return;
  fetch("/api/reading-tick", { body: JSON.stringify({ book_id: BOOK, seconds: 60, page: curPage })});
}
```

- 只要标签页可见就每分钟上报 60 秒 → 用户把书打开放在那儿不做别的事，也会被计为"阅读"。`bookshelf.html:5` 的"🔥 本周阅读 N 分钟"因此**系统性偏高**。
- 没有"无操作就停"的逻辑。建议：记录 `lastInteractionAt`（scroll/pointerdown/keydown 更新），超过 3 分钟无交互则跳过上报：

```javascript
let lastAct = Date.now();
["scroll","pointerdown","keydown"].forEach(ev =>
  addEventListener(ev, () => (lastAct = Date.now()), { passive: true }));
function readingTick() {
  if (document.visibilityState !== "visible") return;
  if (Date.now() - lastAct > 3 * 60000) return;      // 3 分钟无操作不计时
  fetch(...);
}
```

### 3.9 键盘 / 触摸（P2）

- ✅ 方向键、PageUp/PageDown、`a` 切标注模式都实现了（`reader.js:456-461`）。
- ❌ `+` / `-` 没有绑定缩放；`Esc` 不能关书签抽屉/编辑器（`closeEditor` 没有键绑定）。
- ❌ `page-jump` 用 `onchange`（回车或失焦才触发）—— 对 `<input type=number>` 是合理的，但没有 `Enter` 显式绑定（部分浏览器 number 输入回车不触发 change）。
- ❌ 书签抽屉打开后**没有焦点管理**：`toc-btn` 点击后焦点仍在按钮上，Tab 会走到页面其他元素而不是抽屉内；无法用键盘关闭 → 无障碍致命。
- ❌ 单页布局的左右滑动阈值 60px（`reader.js:466`），但 `touchX` 在 `touchstart` 时记录（`passive:true`），标注模式下的拖框用 `pointerdown` —— **两套事件混用**，iOS Safari 上可能互相干扰。

### 3.10 `renderOverlays` 的坐标未按「旋转页」处理（P2）

`reader.js:278-280` 直接用 `a.x0 * 100%` 作为 CSS 百分比。PDF 页面 `vp0` 的 `rotation` 若非 0（扫描件常见 90° 旋转），`getViewport({scale})` 的宽高与 `pageRectNorm` 获取的 `getBoundingClientRect()` 就不一致 → **旋转页的标注位置会整体偏移/错位**。

建议在 `renderOverlays` 里读取 `page.getViewport({scale:1}).rotation`，非 0 时对坐标做变换；或（更省事）在 `renderPage` 里不做旋转归一，把 `viewport.rotation` 记到 `holder.dataset.rot`，overlay 用 CSS `transform: rotate()` 配合。

---

## 4. 可访问性（a11y）—— 系统性缺失（P1/P2）

### 4.1 全站 0 个 `aria-*`（P1）

```text
$ grep -rn "aria-" web/templates/*.html web/static/reader.js web/static/filters.js
（无输出）
```

`read.html` 的 9 个图标按钮（`read.html:23-26, 32-40, 43`）**全部只有视觉符号**：

```html
<button id="bm-btn" class="r-ic" title="收藏本页书签">☆</button>
<button class="r-ic" id="zoom-out">−</button>      ← 无 title、无 aria-label
<button class="r-ic" id="prev-btn">‹</button>      ← 同上
```

- `title` 在大部分屏幕阅读器里**不可靠**（VoiceOver 会读、NVDA 不一定），而且 `title` 不应作为唯一可访问名。
- `zoom-out`/`zoom-in`/`prev-btn`/`next-btn`/`toc-close` 连 `title` 都没有 → **可访问名为空**，屏幕阅读器读作"button"。

**修法**（模板层，逐条加 `aria-label`，10 行）：

```html
<button id="bm-btn" class="r-ic" title="收藏本页书签" aria-label="收藏本页书签" aria-pressed="false">☆</button>
<button id="toc-btn" class="r-ic" title="书签列表" aria-label="书签列表" aria-expanded="false" aria-controls="toc-drawer">📖</button>
<button id="ann-btn" class="r-ic" title="标注模式" aria-label="标注模式" aria-pressed="false">🖍</button>
<button id="theme-btn" class="r-ic" title="纸张主题" aria-label="切换纸张主题">☀</button>
<button class="r-ic" id="zoom-out" aria-label="缩小">−</button>
<button class="r-ic" id="zoom-in" aria-label="放大">＋</button>
<button class="r-ic" id="layout-btn" aria-label="切换布局">⇔</button>
<input id="page-jump" type="number" min="1" aria-label="跳转到页码">
<button class="r-ic" id="prev-btn" aria-label="上一页">‹</button>
<button class="r-ic" id="next-btn" aria-label="下一页">›</button>
```

配套 JS：`setAnnMode` 里同步 `aria-pressed`，`toc-btn` 里同步 `aria-expanded`，`aria-live="polite"` 加到 `#toast`。

### 4.2 对比度不达 WCAG AA（P2）

实测（`style.css:1-5` 的暗色主题）：

| 前景/背景 | 对比度 | 需要 | 结论 |
|---|---|---|---|
| `--tx2 #a4abbb` on `--panel #1a1d25` | **7.32** | ≥4.5 | ✅ |
| `--tx  #e6e8ee` on `--panel` | **13.75** | ≥4.5 | ✅ |
| `--acc #7aa2f7` on `--bg` | **7.31** | ≥4.5 | ✅ |
| `--warn #e0af68` on `--panel` | **8.43** | ≥4.5 | ✅ |
| `--red #f7768e` on `--panel` | **6.37** | ≥4.5 | ✅ |
| **`--tx3 #737b8d` on `--panel #1a1d25`** | **3.97** | ≥4.5 | ❌ **不达标** |

`--tx3` 被 `.mut`（`style.css:67`，12.5px）、`footer`（12.5px）、`.card .m`（11.5px）、`.ev` 大量使用 → **页面上所有"次要信息"都不达 AA**。修法：`--tx3: #8a93a6`（约 5.2:1），或把 `.mut` 的 `font-size` 提到 13.5px 并改用 `--tx2`。

### 4.3 其它 a11y 问题（P2）

| 位置 | 问题 |
|---|---|
| `read.html:5` | `maximum-scale=1.0, user-scalable=no` —— **禁止缩放，WCAG 1.4.4 违规**，低视力用户无法放大。建议删掉这两个属性（PDF 阅读器本身有 zoom，浏览器整页缩放应保留） |
| `library.html:53`、`bookshelf.html:9,33` | `<img alt="">` 三处。卡片封面是**有信息的图片**（书名封面），`alt=""` 会从无障碍树移除。建议 `alt="{{ title }}"`，或保留 `alt=""` 但卡片已有文字题名——**后者其实符合规范**（相邻文字已表达同样内容，空 alt 是最佳实践）。**判为 P2 观察项，不是 bug。** |
| `base.html:30` | `<footer>` 文案"限速采集 N 册已归档"——`N` 与导航栏 `✓ N` 重复，信息噪音 |
| `settings.html:37` | 复选框 `<label>` 包裹但 `input` 无 `id`，label 关联靠包裹（可用）✅ |
| 全站 | 无 `:focus-visible` 样式 → 键盘导航时**看不出焦点在哪**（`style.css` 里只有 `*{margin:0;padding:0}` 和少量 hover）。加一条：`:focus-visible{outline:2px solid var(--acc);outline-offset:2px}` |
| 全站 | 无 skip-link（`base.html` 直接 `<nav>` → `<main>`），键盘用户每次要 Tab 过 5 个导航链接 |
| `read.html:42` | 书签抽屉无 `role="dialog"` / `aria-modal="true"` / 焦点陷阱 → 打开后 Tab 会跑出抽屉 |

---

## 5. 响应式 / CSS / 模板质量

### 5.1 CSS 重复与命名（P2）

- **`style.css` 与 `reader.css` 各自定义了一套 CSS 变量**（`--bg/--panel/--line/--tx/--tx2/--tx3/--acc/--acc2/--warn/--red`），值略有差异（`style.css:2` `--bg:#12141a` vs `reader.css:2` `--bg:#14161c`；`style.css` 有 `--cyan`，`reader.css` 没有）。`reader.css:87` 的注释坦承"read.html 不引 style.css，此处补最小 .btn 样式" —— **这是复制粘贴的必然结果**。
  - 建议：抽出 `/static/tokens.css`（只含 `:root{}`），两边都 `@import` 或都在 HTML 里 `<link>`（无构建链下用两个 `<link>` 最简单）。
- `.mut` 在 `style.css:67` 与 `reader.css:86` **重复定义**。
- 大量内联 `style="..."`（`settings.html:10,12,13,15,42,45,58`；`jobs.html:13,14,15,19,37,45`；`detail.html:8,40`）—— 无构建链时这不算大问题，但 `settings.html` 里同一个 `background:var(--panel2);border:1px solid var(--line);color:var(--tx);border-radius:6px;padding:6px 8px` 出现了 **3 次**，应提成 `.field` 类。
- 媒体查询只有 2 个断点（`860px`、`720px`），平板横屏（768-1024）落在 720 以上 → `.src-grid` 是 4 列，设置页在 iPad 竖屏会挤。加 `@media(max-width:1024px){.src-grid{grid-template-columns:1fr 1fr}}`。

### 5.2 模板重复（P2）

`library.html:6-46` 与 `review.html:6-42` 的**筛选区（表单 + 两组 pillrow）逐行重复**，实测 diff 只有 3 处实质差异（action 地址、是否有 status 下拉、一句提示文案）：

```diff
< <form ... action="/explore">          > <form ... action="/review">
< （有 status 下拉 4 行）               > （无）
< 提示文案含 "LoC 善本跑 loc_fill_details"
```

建议抽 `templates/_filters.html`（带 `{% set action = ... %}` / `{% set show_status = ... %}` 参数）：

```jinja
{# _filters.html —— 传 action、show_status、q_placeholder #}
<form class="filters" method="get" action="{{ action }}">
  ...
  {% if show_status %}<select name="status">...</select>{% endif %}
</form>
{% include "_pills.html" %}
```

**分页片段**在 `library.html:71-77` 与 `review.html:66-72` **完全一致**（含 `q|urlencode` 那一长串），也应抽成 `_pager.html` 宏。

### 5.3 响应式实测结论（P2）

- `/`（书架）：`.grid` 在 720px 以下变 2 列 ✅，hero 卡横向不变 ✅
- `/explore`：`.filters` 在 720px 以下 `input` 变 100% ✅，但 **`.filters select` 没有响应式规则** → 手机上下拉框仍是默认宽度，和 100% 的 input 不对齐
- `/read`（阅读器）：`.r-bar` 里 7 个控件 + 页码输入框，在 375px 宽度下**必然溢出/挤压**（`.r-bar` 无 `flex-wrap`）。建议手机端隐藏 `#page-jump` 和 `#zoom-val`，只留缩放按钮和翻页。
- `/settings`：`.src-grid` 4 列，720px 以下仍是 4 列（`style.css:75` 只对 `860px` 做了 `1fr 1fr`）→ 手机上是 2 列 ✅ 但 input 宽度 100% 挤在 2 列里，`.field` 提出来后要补 `@media`。

---

## 6. 功能完整性 / 信息架构（P2）

### 6.1 `settings.html` 嵌套 `<form>` —— **「保存设置」实际是坏的**（P1，HTML 结构错误）

```html
<!-- settings.html:31 -->
<form method="post" action="/settings">
{% for s in sources %}
  ...
  {% if s['meta_strategy'] == 'snapshot' %}
  <!-- settings.html:63 ← 在外层 form 内部又开了一个 form -->
  <form method="post" action="/upload-snapshot" enctype="multipart/form-data"> ... </form>
  {% endif %}
{% endfor %}
<button class="btn primary" type="submit">保存设置</button>   <!-- settings.html:75 -->
</form>
```

实测源结构：

```text
L48:  <form action="/settings">          ← 外层
L119: <form action="/upload-snapshot">   ← 内层（嵌套，HTML 规范禁止）
L126: </form>                            ← 浏览器解析：这个 </form> 关闭的是 **外层**
L202: </form>                            ← 变成孤立的结束标签，被忽略
enabled_ 复选框 4 个、保存按钮在 L202 之前
```

**浏览器实际解析结果**：外层 form 在 L126 被提前关闭 → **L126 之后的所有 source 的设置控件（enabled/quota/quality/catalog_url）都不属于任何 form** → 点「保存设置」只会提交前 1 个 source 的字段（而且因为 `</form>` 错位，可能一个都不提交）。

> **这条是本次审查里唯一"确定性功能损坏"的 P1**：用户改了配额/开关，点保存，看起来跳转了（303 → /settings），实际上只写了部分 source。

**修法**（推荐：把上传表单移出设置表单，用 `<dialog>` 或独立区块；最省事的是用 HTML5 的 `form=` 属性解耦）：

```html
<!-- 外层设置表单保留 -->
<form method="post" action="/settings" id="settings-form">
  {% for s in sources %}
    ... 控件 ...
    {% if s['meta_strategy'] == 'snapshot' %}
      <button class="btn primary" type="submit"
              formaction="/upload-snapshot" formenctype="multipart/form-data"
              formnovalidate>导入快照 JSON</button>
      <input type="file" name="files" multiple accept=".json" form="upload-form-{{ s['id'] }}">
    {% endif %}
  {% endfor %}
  <button class="btn primary" type="submit">保存设置</button>
</form>

<!-- 每个快照馆一个独立表单，用 id 关联控件外的 input -->
{% for s in sources %}{% if s['meta_strategy'] == 'snapshot' %}
<form method="post" action="/upload-snapshot" enctype="multipart/form-data" id="upload-form-{{ s['id'] }}"></form>
{% endif %}{% endfor %}
```

更干净的做法：**把"导入快照"从 `/settings` 页面挪到 `/review` 页面顶部**（它本来就是这个用途，`upload_snapshot` 成功后也是 `RedirectResponse("/review")`）。这样 settings 页面只做设置，结构问题自然消失。

### 6.2 前后端枚举不同步（P2，见 §3.2 的根因）

`LAYOUTS`/`THEMES`/`QUALITIES`/`ANN_KINDS` 在 `app.py:112-116` 定义，**前端各自硬编码一份**：

| 枚举 | app.py | reader.js | settings.html | 一致性 |
|---|---|---|---|---|
| LAYOUTS | `{scroll,double,single}` | `["scroll","double","single"]` | — | ✅ |
| THEMES | `{light,night}` | `["light","sepia","night"]` | — | ❌ **缺 sepia** |
| QUALITIES | `{auto,pdf,orig,mid,thumb}` | — | `['auto','pdf','orig','mid','thumb']` | ✅（人工同步） |
| ANN_KINDS | `{highlight,underline,note}` | `{highlight,underline,note}` | — | ✅ |

建议：前端通过 `read.html` 的 `data-*` 或 `<script type="application/json" id="enums">` 注入：

```html
<script type="application/json" id="enums">{{ {"themes": themes, "layouts": layouts, "kinds": kinds}|tojson }}</script>
```

`reader.js` 读 `JSON.parse($("enums").textContent)` —— 一次注入，永不失步。

### 6.3 缺失的 UX 要素（P2）

| 页面 | 缺什么 |
|---|---|
| 全部 | **无 loading 态**：`/explore`、`/review` 走服务端渲染（有白屏期但可接受）；`/read` 的 PDF.js 加载期间 `#pages` 是空的，用户看到黑屏（`init()` 里 `showError` 只在**失败**时触发，加载中无提示）。建议 `init()` 开头 `pagesEl.innerHTML = '<div class="load-error">正在加载 PDF…</div>'` |
| `/explore` | 无"清空全部筛选"按钮（只有逐个 ✕）；`page` 超出范围时静默显示空状态（"没有符合条件的书目"）而不是提示"页码超出范围" |
| `/review` | 批量操作后 `location.reload()` 硬刷新（丢失滚动位置/筛选状态？筛选在 URL 里所以保留了 ✅）；**无"操作结果条数"反馈**（`r.ok` 就 reload，用户不知道发生了什么）；`ids` 上限未做（勾 500 本 = 500 次事务） |
| `/jobs` | `retry()` 失败只把按钮文字改成"失败"，无原因；无自动刷新（用户要手动 F5 看进度）；`by_source` 进度条三段（done/queued/failed）**没有图例**，颜色含义要猜 |
| `/`（书架） | "已归档"tab **一次渲染 1000 册**（`app.py:211`），实测 **317KB HTML**（1001 册时 0.08s，但 NAS 上 ARM CPU + 慢网会明显变慢）；无分页/虚拟滚动 |
| `/read` | 无"目录/大纲"（PDF outline 存在 `doc.getOutline()` 里，白白浪费）；书签抽屉就是唯一导航 |
| 全站 | 无"最近阅读"跨设备同步冲突提示（同一本书两台设备同时读，后保存覆盖前者，无提示） |

### 6.4 性能：N+1 文件系统调用（P2）

`book_urls()`（`app.py:61-80`）**每册书**都做 `os.path.isdir` + `os.listdir` + `os.path.exists`：

```python
if os.path.isdir(directory): names = sorted(n for n in os.listdir(directory) ...)
cover_exists = os.path.exists(os.path.join(directory, "cover.jpg"))
```

- `/explore`：24 册 × 3 次 = 72 次 FS 调用（本地实测 5ms，可接受）
- `/` 已归档 tab：**1000 册 × 3 = 3000 次 FS 调用**（实测 80ms/317KB，NAS 机械盘上会显著变慢）

建议：①书架"已归档"分页（每页 60）；②`book_urls` 的结果按 `book.id` 做 30s 进程内缓存（**注意**：下载完成后要失效，否则新书看不到封面 —— 现有 `_FACETS_CACHE` 也有同样问题，下载完新书后分类要等 60s 才出现）。

---

## 7. 代码质量：`app.py` 是否该拆分

**结论：该拆，但不要用大炮。**

现状 761 行里混了 6 类职责：

| 职责 | 行号 | 行数 |
|---|---|---|
| 应用装配（mount/模板/过滤器） | 31-180 | ~150 |
| HTML 页面路由（6 个） | 182-315, 454-473, 564-622 | ~210 |
| JSON API（15 个） | 317-452, 476-561, 724-761 | ~250 |
| 后台线程 / 同步下载 | 496-518, 537-560 | ~40 |
| 上传 + 导入 | 653-721 | ~70 |
| 存储面板 / 工具函数 | 158-180, 590-622 | ~60 |

推荐拆法（**保持不变 `web.app:app` 这个入口**，`entrypoint.sh` 不用改）：

```text
web/
  app.py          # 仅：FastAPI 实例、mount、templates、中间件、include_router  (~120 行)
  deps.py         # get_db / common_ctx / 过滤器注册
  constants.py    # LAYOUTS/THEMES/QUALITIES/ANN_KINDS/COLLECTION_LABELS（前后端共享的唯一真源）
  routers/
    pages.py      # / /explore /books/{id} /read/{id} /review /jobs /settings
    reader_api.py # /api/progress /api/bookmarks /api/annotations /api/reading-tick
    admin_api.py  # /api/review /api/retry /api/fetch /api/links /api/doctor /upload-snapshot
  services/
    storage.py    # book_urls / _dir_size / 缓存
```

另外几条具体的代码问题：

| 位置 | 问题 | 建议 |
|---|---|---|
| `app.py:26` | `from core.pipeline import ... _dest_dir` —— 跨模块 **import 私有函数**（下划线开头）。core 重构时会静默断裂 | 在 `core/pipeline.py` 里导出 `dest_dir`（公开名），app 只用公开名 |
| `app.py:15` | `from typing import List, Optional`，`List` 用于 `files: List[UploadFile]` ✅、`Optional` 用于 `_DB: Optional[DB]` ✅ | 都用了，无冗余 |
| `app.py:83-93` | `_FACETS_CACHE` 是**无上限 dict**（按 source 分 key，最多 4 个，可接受）但**从不失效** → 下载新书后分类要等 60s | 在 `api_review`/`upload_snapshot`/`fetch_one` 完成后 `_FACETS_CACHE.clear()` |
| `app.py:119` `_ann_fields(body: dict) -> tuple` | 无类型标注（`tuple` 太宽） | `-> tuple[str, str]` |
| `app.py:158` `_bytes_h(n: int)` | `n` 可能为 `None`（`total_bytes=agg["b"]` 用 `COALESCE` 保证非 None ✅） | — |
| `app.py:100` `common_ctx` | 每个页面都查 `SELECT * FROM sources` + `count_by_status()`（2 次查询），`/jobs` 又额外 5 次 → 模板上下文膨胀 | `sources` 也可缓存（60s），`counts` 有必要实时 |
| `app.py:590` `_dir_size` | `/settings` 每次请求 `os.walk` 整个 `books/` + `snapshots/` + `logs/` —— **书一多就秒级** | 加 30s 缓存，或改用 `os.statvfs` |
| `web/templates/read.html:46` | `{% if bm['note'] %}· {{ bm['note'] }}{% endif %}` ✅ 自动转义，但书签 note 是后端截断 300 字符的，前端未做省略号截断 → 长备注撑破抽屉 | CSS `overflow:hidden;text-overflow:ellipsis;white-space:nowrap` |
| `web/static/reader.js:18` | `renderTasks = new Map()` 用 page number 做 key ✅；但 `rerenderAll()` 里 `renderTasks.clear()` 后若 `renderPage` 的 `await` 还没 resolve，回调里 `renderTasks.get(p) !== entry` 会正确 return ✅ | 无问题（这块写得好） |
| `web/static/reader.js:19` | `heartbeat` 变量赋值后从未 `clearInterval`（页面 unload 自然清） | 无问题，但 `exitFlush` 里顺手 `clearInterval(heartbeat)` 更干净 |
| `web/static/reader.js:38` | `anns = JSON.parse(root.dataset.anns \|\| "[]")` —— 如果后端 someday 注入非法 JSON 会抛错，已被 `try{}catch` 包住 → `showError` 显示"PDF 加载失败"，**错误归因错误**（其实是标注数据坏了） | 拆成两个 try，标注解析失败时 `anns=[]` 继续 |

---

## 8. 静态资源 / 自托管 PDF.js

### 8.1 文件完整性：**完整**（不是问题）

```text
pdf.min.mjs          329,881 bytes   version "4.6.82"   尾部: export{...};  括号平衡 ✅
pdf.worker.min.mjs 1,362,587 bytes   尾部: export{yg as WorkerMessageHandler};  ✅
pdf_viewer.min.css    81,560 bytes
```

`reader.js:3` `import * as pdfjsLib from "/static/pdfjs/pdf.min.mjs"` + `reader.js:5` `GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.min.mjs"` —— **主包与 worker 版本一致**（都是 4.6.82），否则 PDF.js 会报 "The API version X does not match the Worker version Y"。

### 8.2 问题清单

| 级别 | 位置 | 问题 | 建议 |
|---|---|---|---|
| **P2** | `web/static/pdfjs/` 目录 | **无版本标识文件、无 LICENSE、无 sha256 清单**。三年后没人知道这是 4.6.82 从哪来的、有没有被改过 | 加 `web/static/pdfjs/VERSION`（一行 `4.6.82`）+ `SHA256SUMS` + 把 pdfjs 的 `LICENSE` 复制过来（Apache-2.0 要求保留） |
| **P2** | `read.html:7` | **`pdf_viewer.min.css` 是死资源**：`reader.js` 完全不使用 `PDFViewer` 组件（只 `import * as pdfjsLib` 然后用 `getDocument`+`page.render` 手搓 canvas），`grep -n "pdfViewer\|textLayer" reader.js` **零命中**。81KB 白下载，且其 `.pdfViewer`/`.textLayer` 规则可能影响布局 | 删掉这行 `<link>`（同时删文件）。**预期收益：首屏少 81KB** |
| **P2** | `reader.js` 无 textLayer | 阅读器**没有文字层** → 不能选词、不能搜索、不能复制。这是功能取舍（省内存），但"标注"功能靠拖框选择，用户体验会差（无法精确框住一行字）。`PDF.js TextLayer` 是现成的，按需（进入标注模式时）加载即可 | 标注模式下 `page.getTextContent()` + `TextLayer` 渲染文字层，让用户能选中文字再高亮（顺带能填 `annotation.text`） |
| **P2** | 全部 `/static/*` 响应头 | 实测只有 `last-modified` + `etag`，**无 `Cache-Control`** → 浏览器用启发式缓存，行为不确定；`?v=` 查询串是手工维护的（`?v=20260920a`），**改了 JS 忘了改版本号 → 用户拿旧缓存**（这是本仓库已经踩过的坑：`b34b78b`/`f8bb7a1` 都是"改完不生效"类修复） | ①给 `/static` 加 `Cache-Control: public, max-age=31536000, immutable` 并**强制所有引用带 `?v=`**（当前已是）；②版本号别手工维护，用 `app.mount` 前计算 `static_mtime` 注入模板变量 `{{ asset_v }}` |
| **P2** | `read.html:55` | `<script type="module" src="/static/reader.js?v=...">` —— 无 `integrity`（SRI）。自托管+同源，SRI 意义不大；但**没有 CSP 时**，if 有人能在 `/static` 写文件（例如通过 §1.5 的上传，虽然上传目录在 `snapshots/`）就能直接执行 | 与 §1.6 的 CSP 合并考虑 |
| **P2** | `pdfjs` 无 `cMapUrl` | 中文古籍 PDF 常用 CJK CMap（`UniGB-UCS2-H` 之类）。没有 cMap 时**部分中文 PDF 的文字层会乱码**。当前无文字层所以还没暴露，一旦加 textLayer 就会遇到 | 需要 `cmaps/` 目录（~30 个 .bcmap 文件，约 1MB），`getDocument({cMapUrl:'/static/pdfjs/cmaps/', cMapPacked:true})` |

### 8.3 建议的 Cache-Control 实现

```python
# web/app.py
class VersionedStatic(StaticFiles):
    """自托管静态资源：一年强缓存 + immutable；版本号由启动时计算。"""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

app.mount("/static", VersionedStatic(directory=...), name="static")
```

配合模板里 `{{ asset_v }}`（启动时取 `web/static` 下最大 mtime 的 8 位哈希）。

---

## 9. 前端测试 —— 完全没有（P1）

```text
$ ls tests/  → test_regressions.py（后端脚本式断言）
$ grep -rn "reader.js\|filters.js\|escapeHtml\|jsdom\|playwright\|vitest" tests/
（无输出）
```

前端 **0 行测试**。而本报告里 **3 个 P1 级 bug（§3.1 data-layout、§3.2 sepia、§6.1 嵌套 form）都是纯前端/模板问题，一次 5 行的冒烟测试就能发现**。

**建议（不引入 Node 构建链，用 Python 侧最便宜的方式）：**

### 9.1 模板结构断言（纯 Python，零依赖，30 行）

```python
# tests/test_web_templates.py
"""模板结构回归：不启动服务，直接渲染 + 字符串断言。"""
import re, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EIGHTNATIONS_DATA", "/tmp/pytest-eight")

from fastapi.testclient import TestClient
from web.app import app

client = TestClient(app)

def test_settings_has_no_nested_form():
    """settings.html 曾出现嵌套 <form> 导致保存按钮失效（审查 B §6.1）。"""
    html = client.get("/settings").text
    depth = 0
    for m in re.finditer(r"<form\b|</form>", html):
        depth += 1 if m.group().startswith("<form") else -1
        assert depth <= 1, f"settings 出现嵌套 form（位置 offset={m.start()}）"
    assert depth == 0, "form 标签未配平"

def test_theme_enum_synced():
    """后端 THEMES 必须覆盖 reader.js 循环的所有主题（审查 B §3.2）。"""
    from web.app import THEMES
    js = open("web/static/reader.js", encoding="utf-8").read()
    loop = re.search(r"const THEME_ORDER = \[(.*?)\]", js)
    if loop:
        for t in re.findall(r'"(\w+)"', loop.group(1)):
            assert t in THEMES, f"主题 {t} 不在后端白名单 THEMES 中"

def test_reader_layout_dataset_written():
    """setLayout 必须写 root.dataset.layout，否则 data-layout 相关 CSS 全失效（审查 B §3.1）。"""
    js = open("web/static/reader.js", encoding="utf-8").read()
    assert "root.dataset.layout" in js, "reader.js 从未给 #reader 设置 data-layout"

def test_no_safe_filter_in_templates():
    """禁止 |safe（审查 B §1.1 的回归基线）。"""
    for f in os.listdir("web/templates"):
        assert "|safe" not in open(f"web/templates/{f}", encoding="utf-8").read(), f

def test_bad_json_returns_400_not_500():
    """非法 JSON 必须 400（审查 B §2.1）。"""
    for ep in ("/api/bookmarks", "/api/annotations", "/api/reading-tick",
               "/api/review", "/api/retry"):
        r = client.post(ep, content=b"{bad", headers={"content-type": "application/json"})
        assert r.status_code == 400, f"{ep} 返回 {r.status_code}（应为 400）"
```

### 9.2 前端行为测试（可选，Playwright，~1 个文件）

如果愿意引入 `playwright`（仅测试依赖，不进生产镜像）：

```python
def test_layout_switch_changes_dataset(page):
    page.goto(f"{BASE}/read/1")
    page.click("#layout-btn")
    assert page.get_attribute("#reader", "data-layout") == "double"   # ← 现有代码会失败
```

**这是最有价值的一条**：它直接锁住 §3.1。

---

## 10. 前端整体评价（分维度打分）

| 维度 | 分 | 依据 |
|---|---|---|
| **XSS / 注入防护** | 7.5 | autoescape ✅、零 `|safe` ✅、`tojson` 已转义 ✅、reader.js 两处 innerHTML 都有转义 ✅、颜色双白名单 ✅。扣分：全链路靠"约定"而非机制，无 CSP |
| **鉴权 / 越权 / CSRF** | 2.0 | 零鉴权（设计选择但未显式化）、零 CSRF 防护、IDOR 不校验。务实上危害有限（无会话），但工程上完全缺位 |
| **SSRF / 出网安全** | 3.0 | `catalog_url` 任意 URL 直通 scheduler（实测可写 `169.254.169.254`），域名白名单/私网校验全无 |
| **API 健壮性** | 4.0 | 5 个接口非法 JSON → 500（实测）；text/page 无上限（实测 10MB 入库）；`/read?p=` 零校验；错误码语义混用（400/404/415/422/500 无统一约定） |
| **reader.js 正确性** | 6.0 | 竞态/泄漏处理**优于预期**（3 处身份校验 + cancel + disconnect）；但 data-layout 不写入（确定性 bug）、sepia 被吞、无 resize、single 模式 IO 冗余渲染 |
| **reader.js 可维护性** | 7.0 | 475 行组织清晰、函数短、注释到位（"直接 def 走线程池"那类注释很好）、helper 抽得准 |
| **可访问性 a11y** | 2.5 | **0 个 aria-***、5 个图标按钮无可访问名、`user-scalable=no`、无 focus-visible、`--tx3` 对比度 3.97 不达标 |
| **响应式 / CSS** | 6.0 | 双断点可用、移动端 nav 滚动条处理得当；CSS 变量重复定义两套、`pdf_viewer.min.css` 死资源、`.r-bar` 手机溢出 |
| **模板质量** | 5.5 | 空状态齐全（每个 `{% if %}` 都有 `{% else %}` 人话提示 👍）；但 library/review 筛选区 90% 重复、分页片段完全重复、**settings 嵌套 form（功能损坏）** |
| **性能（前端）** | 5.5 | 静态资源无 Cache-Control、书架一次渲染 1000 册 317KB、N+1 文件系统调用、无文字层（省内存但也砍了选词）；PDF.js 懒渲染（IO + 300% rootMargin）设计得不错 |
| **工程化** | 2.0 | 前端 0 测试、无 lint、`?v=` 手工维护、pdfjs 无版本/校验文件、no CSP |
| **综合** | **5.1 / 10** | 代码写得比典型个人项目好（注释诚实、边界意识强、知道"async 里别跑同步重活"），**但安全工程是"点状修复"而不是"机制化防护"** |

---

## 11. 优先级 Top 5（按"修一条的收益/成本比"排序）

### 🥇 Top 1 — 修 `settings.html` 嵌套 `<form>`（§6.1）
- **成本**：10 分钟（把上传表单移到大 form 外，或用 `form=` 属性 + `formaction`）
- **收益**：**恢复"保存设置"这个核心功能**。现在改配额/开关/目录 URL 只在部分情况下生效，而这是整个采集管线的控制面板。
- **验证**：`curl` 提交 /settings 后查库，确认所有 source 的字段都变了；或加 §9.1 的 `test_settings_has_no_nested_form`。

### 🥈 Top 2 — `_json_body()` 统一 JSON 解析 + 输入上限（§2.1 + §2.2）
- **成本**：30 分钟（1 个 helper + 5 处调用替换 + 6 个字段加 `[:N]`/`min/max`）
- **收益**：消灭 5 个 500 入口、堵住 10MB 文本入库、让前端能拿到可判断的 400 而不是 HTML 错误页。**这是"服务是否健壮"的分水岭。**

### 🥉 Top 3 — `catalog_url` 域名白名单 + `HttpClient` 私网校验（§1.3）
- **成本**：1 小时（含确认哪些站点域名要放行、确认重定向行为）
- **收益**：切断唯一的"从 Web 面打穿到内网"的路径。这是本审查里**安全危害最大**的一条。
- **验证**：`catalog_url=http://127.0.0.1:8080/` 应被拒绝；`catalog_url=https://gallica.bnf.fr/...` 应通过。

### 4️⃣ Top 4 — 阅读器 3 连修：`data-layout` + `sepia` + `resize`（§3.1/3.2/3.3）
- **成本**：20 分钟（3 处、合计 ~15 行）
- **收益**：直接修好 3 个用户能感知的功能缺陷（布局切换、护眼主题、手机横竖屏）。**性价比最高**。

### 5️⃣ Top 5 — 无鉴权假设显式化 + 只读开关 + 启动告警（§1.4）
- **成本**：30 分钟（启动 print + compose 注释 + 10 行只读中间件）
- **收益**：把"没鉴权"从一个**沉默的假设**变成**被文档化、可开关、有告警**的显式设计决策。这比"加一个半吊子登录"更符合项目定位，也避免了将来被当成漏洞发现时的争议。

> **不建议进 Top 5 但值得排期**：`/static` 的 Cache-Control（§8.2，20 行，收益立竿见影）；删除 `pdf_viewer.min.css`（§8.2，减 81KB 首屏）；模板抽 `_filters.html`/`_pager.html`（§5.2，纯重构）。

---

## 12. 上线前必做安全检查清单

> 面向"部署到 NAS、只在内网用"的真实场景。**逐项打勾，未打勾的项必须在 README 的部署章节写清风险。**

### A. 网络暴露面（最高优先）

- [ ] **A1** `docker-compose.yml` 的 `ports` 不要写 `0.0.0.0` 暴露——改成 `"${EIGHTNATIONS_BIND_ADDR:-IP}:8080:8080"`，只绑局域网网卡
- [ ] **A2** 确认 QNAP 上没有反向代理/端口转发把 8080 映射到 WAN（`Web Station`、`反向代理`、`DDNS` 三处都查）
- [ ] **A3** 确认 NAS 防火墙拒绝来自 WAN 的 8080 入站（QNAP `控制台 → 安全 → 防火墙`）
- [ ] **A4** 给 `EIGHTNATIONS_ALLOWED_HOSTS` 设置 NAS 的实际访问地址（防 DNS rebinding，见 §1.2）
- [ ] **A5** 若确实需要给非本机设备访问，**先启用** `EIGHTNATIONS_READ_ONLY=1` 或 Token，再开放

### B. 无鉴权模型（必须显式化）

- [ ] **B1** 在 `README.md` 的"部署"章节顶部加一段醒目警告：本应用无登录、任何能访问该端口者拥有完整管理权限
- [ ] **B2** 启动日志打印"⚠️ 无鉴权模式"（§1.4 建议 ①）
- [ ] **B3** `docs/DESIGN.md:198` 的"可选简单 Token"要么实现、要么从文档删掉（**文档说谎比没文档更糟**）
- [ ] **B4** 关闭 `/openapi.json`（`openapi_url=None`，§1.6）

### C. 注入与 XSS（已基本到位，做回归基线）

- [ ] **C1** `grep -rn "|safe" web/templates/` 必须为空（加进 §9.1 测试）
- [ ] **C2** `templates.env.autoescape is True` 必须有测试断言（防有人改 `Jinja2Templates` 参数）
- [ ] **C3** `jp2t` / `bytes_h` / `col_label` 等自定义过滤器**必须继续返回 `str`**——若返回 `Markup` 即打开存储型 XSS（在 `core/text.py` 加注释警告）
- [ ] **C4** reader.js 新增任何 `innerHTML` 拼接时，必须过 `escapeHtml`/`safeColor`（加 `grep -c "innerHTML"` 人工 review 习惯）
- [ ] **C5** （可选，加分）加 CSP 报告模式：`Content-Security-Policy-Report-Only: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'` —— 先只报告不拦截，跑一周看有没有遗漏，再转强制。**注意**：现在有内联 `onclick`（`review.html:45-48`、`jobs.html:37`）和 3 个内联 `<script>`（`review/jobs/settings`），所以必须带 `'unsafe-inline'`；要真正收紧得先把这 4 处内联事件改成 `data-*` + 事件委托（`filters.js` 已经是这个模式的样板）

### D. SSRF / 出网

- [ ] **D1** `catalog_url` 域名白名单（§1.3）
- [ ] **D2** `HttpClient.get/download` 拒绝私网/回环/链路本地 IP，且 `allow_redirects=False` 或逐跳校验（§1.3）
- [ ] **D3** 测试：把 `catalog_url` 设为 `http://127.0.0.1:8080/api/doctor`，确认调度器**不会**去请求

### E. 输入与上传

- [ ] **E1** 所有 `await request.json()` 走 `_json_body()`，非法 JSON 返回 400（§2.1）
- [ ] **E2** `annotation.text` ≤ 2000、`bookmark.note` ≤ 300、`page` 落在 `[1, page_count+50]`、`review.ids` ≤ 500（§2.2）
- [ ] **E3** `/read?p=` clamp 到 `[1, total_pages]`（§2.2）
- [ ] **E4** 上传：拒绝 `.` 开头的隐藏文件名、导入成功后 `rmtree` 临时目录、`import_snapshot_files` 改 `def` 走线程池（§1.5）
- [ ] **E5** 确认 `data/snapshots/` 有磁盘配额或定期清理（现在每次上传留一个目录，永不清）

### F. 数据与静态资源

- [ ] **F1** 备份 `data/db/library.db` 与 `data/books/`（无鉴权下"误操作/恶意操作"的兜底只能是备份）
- [ ] **F2** `/static` 加 `Cache-Control: immutable`，`?v=` 改成自动计算（§8.2）——**避免"改了 JS 用户拿旧缓存"这类反复出现的坑**
- [ ] **F3** pdfjs 目录加 `VERSION` + `SHA256SUMS`，保留 Apache-2.0 LICENSE（§8.2）
- [ ] **F4** 删除未使用的 `pdf_viewer.min.css`（-81KB）或改用 `PDFViewer` 组件（引入 textLayer，获得选词能力）

### G. 可访问性 / 可用性（上线"能用"的最低线）

- [ ] **G1** 阅读器 9 个图标按钮补 `aria-label`（§4.1，10 行）
- [ ] **G2** 删除 `user-scalable=no`（§4.3，WCAG 1.4.4）
- [ ] **G3** `--tx3` 提亮到 ≥4.5:1（§4.2）
- [ ] **G4** 手机竖屏（375px）实际打开 `/read` 确认底部工具栏不溢出

### H. 回归测试（防"修好又坏"）

- [ ] **H1** 落地 §9.1 的 6 个纯 Python 测试（无嵌套 form / 枚举同步 / data-layout / 无 |safe / 非法 JSON 400 / autoescape）
- [ ] **H2** 这些测试接进 CI（D 审查的 `docker-publish.yml` 已经在跑回归测试，加进同一个 `tests/` 目录即可）
- [ ] **H3** 可选：Playwright 冒烟测试锁住 §3.1（布局切换）

---

## 附录 A：本次实测命令与结果（可复现）

```bash
# 启动（/tmp 数据目录，不碰仓库 data/）
EIGHTNATIONS_DATA=/tmp/reviewB python3 -m uvicorn web.app:app --port 8099

# 1) 鉴权：全部 200，无 cookie
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/settings
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8099/api/links

# 2) 非法 JSON → 5 个 500
for ep in /api/bookmarks /api/annotations /api/reading-tick /api/review /api/retry; do
  curl -s -o /dev/null -w "$ep %{http_code}\n" -X POST http://127.0.0.1:8099$ep \
    -H 'Content-Type: application/json' -d '{bad json'
done
# → 全部 500

# 3) SSRF：写入元数据地址
curl -s -o /dev/null -X POST http://127.0.0.1:8099/settings \
  --data-urlencode "enabled_na_jp=on" --data-urlencode "quota_na_jp=10" \
  --data-urlencode "quality_na_jp=auto" --data-urlencode "catalog_url_na_jp=http://169.254.169.254/"
# → 303，库内 catalog_url = 'http://169.254.169.254/'

# 4) 输入无上限
curl -X POST http://127.0.0.1:8099/api/annotations -H 'Content-Type: application/json' \
  -d "{\"book_id\":1,\"page\":1,\"x0\":0.1,\"y0\":0.1,\"x1\":0.5,\"y1\":0.5,\"text\":\"$(python3 -c 'print("A"*10000000)')\"}"
# → 200（10MB 入库）

# 5) sepia 被吞
curl -X POST http://127.0.0.1:8099/api/progress/1 -H 'Content-Type: application/json' \
  -d '{"page":5,"theme":"sepia","layout":"double","zoom":1.5}'
# → 查库 theme='light'

# 6) 路径穿越全部被拦（basename 生效）
curl -F source=loc -F "files=@/tmp/collection_a.json;filename=../../evil.json" \
  http://127.0.0.1:8099/upload-snapshot     # → 落盘 <uuid>/evil.json，未逃出
curl -s --path-as-is -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:8099/data/../db/library.db   # → 404

# 7) 静态资源无 Cache-Control
curl -sI http://127.0.0.1:8099/static/pdfjs/pdf.min.mjs | grep -i cache-control   # → 无输出

# 清理
pkill -f "uvicorn web.app:app --port 8099" && rm -rf /tmp/reviewB
```

## 附录 B：P0/P1/P2 全量索引

| # | 级别 | 位置 | 问题 | 章节 |
|---|---|---|---|---|
| 1 | **P1** | `web/templates/settings.html:63` | 嵌套 `<form>` → 「保存设置」实际失效 | §6.1 |
| 2 | **P1** | `web/app.py:346,372,393,408,479,524` | `await request.json()` 无 try → 5 个接口非法 JSON 返回 500 | §2.1 |
| 3 | **P1** | `web/app.py:386,348,324,482` / `web/app.py:307` | `text` 10MB 入库、`page` 无范围、`/read?p=` 零校验 | §2.2 |
| 4 | **P1** | `web/app.py:390-402` | PATCH/DELETE 标注/书签不校验存在性与归属 | §2.3 |
| 5 | **P1** | `web/app.py:641` + `scheduler.py:88` + `sites/na_jp.py:170` | `catalog_url` 任意 URL → SSRF（实测可写云元数据地址） | §1.3 |
| 6 | **P1** | `web/app.py` 全局 + `docker-compose.yml:10` | 零鉴权且假设未显式化、启动无告警、绑 0.0.0.0 | §1.4 |
| 7 | **P1** | `web/static/reader.js:220-224` | `root.dataset.layout` 从不赋值 → 布局 CSS 全失效 | §3.1 |
| 8 | **P1** | `web/app.py:115` | `THEMES` 缺 `sepia` → 护眼主题静默失效（实测） | §3.2 |
| 9 | **P1** | `web/templates/read.html` 全局 | 0 个 `aria-*`，9 个图标按钮无可访问名 | §4.1 |
| 10 | **P1** | `tests/` | 前端 0 测试（3 个 P1 bug 本可被冒烟测试挡住） | §9 |
| 11 | P2 | `detail.html:31` | `meta.files[0]` 无防御 → IndexError 500 | §1.1.3 |
| 12 | P2 | `settings.html:15` + `core/mounts.py` | 迁移命令直接 `curl\|sh` main 分支脚本，无校验 | §1.1.3 |
| 13 | P2 | 全局 | 无 CSP（内联 onclick ×4 + 内联 script ×3 需先改造） | §1.2 / §12-C5 |
| 14 | P2 | `app.py:692` | 上传接受 `.json` 以外的 `..json` 等隐藏名，且临时目录永不清理 | §1.5 |
| 15 | P2 | `app.py:715` | `async def` 里同步跑 `import_snapshot_files` → 卡事件循环 | §2.5 |
| 16 | P2 | `app.py:37` | `/openapi.json` 未关闭 | §1.6 |
| 17 | P2 | `core/db.py:405` | `LIKE` 未转义 `%`/`_` | §1.7 |
| 18 | P2 | `app.py:608-615,730` | `/settings` 泄露宿主机路径、`/api/doctor` 暴露内部状态 | §1.8 |
| 19 | P2 | `reader.js` 全局 | 无 `resize` 处理 → 横竖屏后 canvas 尺寸错误 | §3.3 |
| 20 | P2 | `reader.js:68-76` | `single` 布局下 observe 全部占位 → 一次翻页渲染 N 页 | §3.4 |
| 21 | P2 | `reader.js:150-257,335-452` | fetch 全链路无错误处理，失败静默 | §3.5 |
| 22 | P2 | `reader.js:404` vs `app.py:377` | 标注阈值前端 2%/后端 1% 不一致 → 静默丢弃 | §3.6 |
| 23 | P2 | `reader.js:446` | 书签 `note` 前端无入口（半成品功能） | §3.7 |
| 24 | P2 | `reader.js:168` | 阅读心跳无"无操作暂停"→ 时长虚高 | §3.8 |
| 25 | P2 | `reader.js` | 无 `+`/`-` 缩放键、`Esc` 不关抽屉、抽屉无焦点陷阱 | §3.9 |
| 26 | P2 | `reader.js:278` | 旋转页标注坐标未做变换 | §3.10 |
| 27 | P2 | `style.css:3` | `--tx3` 对比度 3.97:1 未达 AA | §4.2 |
| 28 | P2 | `read.html:5` | `user-scalable=no` 违反 WCAG | §4.3 |
| 29 | P2 | 全局 | 无 `:focus-visible`、无 skip-link | §4.3 |
| 30 | P2 | `style.css` vs `reader.css` | 两套重复 CSS 变量 + `.mut` 重复定义 + 内联 style 泛滥 | §5.1 |
| 31 | P2 | `library.html:6-77` vs `review.html:6-72` | 筛选区/分页片段 ~90% 重复 | §5.2 |
| 32 | P2 | `app.py:211` | 书架"已归档"一次渲染 1000 册（实测 317KB） | §6.3 |
| 33 | P2 | `app.py:115-116` 等 | 前后端枚举各自硬编码，已发生不同步 | §6.2 |
| 34 | P2 | `app.py:61-80` | `book_urls` N+1 文件系统调用 | §6.4 |
| 35 | P2 | `app.py:26` | import 私有函数 `_dest_dir` | §7 |
| 36 | P2 | `app.py:83-93` | `_FACETS_CACHE` 从不清除 → 新书分类延迟 60s | §7 |
| 37 | P2 | `app.py:590-599` | `/settings` 每次 `os.walk` 全目录 | §7 |
| 38 | P2 | `web/static/pdfjs/` | 无 VERSION/LICENSE/SHA256SUMS | §8.2 |
| 39 | P2 | `read.html:7` | `pdf_viewer.min.css` 是死资源（81KB 白下载） | §8.2 |
| 40 | P2 | `reader.js` | 无 textLayer → 不能选词/搜索/复制 | §8.2 |
| 41 | P2 | `/static/*` | 无 `Cache-Control`，`?v=` 手工维护易漏 | §8.2 |

---

*报告结束。所有 P1 结论均有实测或代码行级证据；未修改任何业务代码（唯一写入文件：`docs/review/B-web.md`）；测试服务已 kill，临时数据在 `/tmp/reviewB`。*
