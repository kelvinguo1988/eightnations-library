# 八国联军图书馆 · 全量代码审查总报告

> 审查日期：2026-10-27
> 审查方式：4 名审查员分区并行（A 存储管线 / B Web 前端 / C 采集调度 / D 测试部署）+ Lead 独立复核与交叉验证
> 代码基线：`37da6aa`（main 分支，工作区干净）
> 基线实测：`python3 tests/test_regressions.py` → 13 组全过（0.37s）；`compileall` 退出码 0；`pyflakes` 仅 5 处未用导入
> 业务代码**零改动**，未 commit；所有复现脚本在 `/tmp`，真实库 `data/db/library.db` 仅只读访问

分报告：**[A-core.md](A-core.md)** · **[B-web.md](B-web.md)** · **[C-adapters.md](C-adapters.md)** · **[D-testdeploy.md](D-testdeploy.md)**

---

## 0. 一句话结论

**工程纪律明显高于同级个人项目（XSS 已真收口、SQL 全程参数化、路径穿越实测全拦、租约模型设计正确），但三条"正确性生命线"都存在实质缺陷：下载完整性靠体积猜、目录收割进度语义错配、设置保存静默失效；而测试套件对这三条全部无感知（8 次关键变异 5 次测不出）。当前生产数据是健康的 —— 下列问题都是"下一次就会踩"，不是"正在丢数据"。**

| 维度 | 评分 | 判断 |
|---|---|---|
| 安全 | 6.5 / 10 | XSS 已收口；剩 SSRF + 无鉴权假设未显式化；输入无上限 |
| 数据正确性 | **4.5 / 10** | 下载完整性靠 min_bytes 猜、弱成功语义、目录进度封存 |
| 框架/扩展性 | 5.5 / 10 | 下载内核抽象好；注册/能力声明/契约校验三缺，新增一馆改 9 处 |
| 前端质量 | 6.5 / 10 | 竞态防护好于预期；3 个确定性功能 bug；a11y 系统性缺失 |
| 可测试性 | **3.5 / 10** | 单脚本断言、无 pytest、Web/前端零覆盖、变异命中率 3/8 |
| 部署/运维 | 6 / 10 | Dockerfile 干净；healthcheck 恒真、信号不转发、脚本无 dry-run |
| 文档一致性 | 5 / 10 | 抽查 28 处，19 处不一致（DESIGN 有整段未实现的旧设计） |
| **综合** | **5.4 / 10** | 骨架是好的，缺的是"证据链"和"守卫" |

---

## 1. 优先级总表（跨报告去重后）

按"修一条能消除多少下游风险"排序。每条都给出**位置 + 影响 + 最小修法**。

### P0 — 下一次就会踩（数据损坏 / 功能静默失效）

| # | 问题 | 位置 | 影响 | 最小修法 |
|---|---|---|---|---|
| **1** | **设置页「保存」对多数来源不生效** | [settings.html:63](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/templates/settings.html#L63) 嵌套 `<form>`（外层 31 行、内层 63–70） | 浏览器在外层遇到第一个 `</form>` 就截断，内层之后渲染的 source 开关/配额/目录 URL **不属于任何 form**，改完点保存＝白改 | 把上传表单移到外层 `<form>` 之外（或改用 `form="upload-form"` 属性关联） |
| **2** | **目录收割进度语义错配 → 目录被提前 7 天封存** | [na_jp.py:220](file:///Users/sec-t/Documents/coding/八国联军图书馆/sites/na_jp.py#L220) `exhausted = fetched < budget`；[scheduler.py:100](file:///Users/sec-t/Documents/coding/八国联军图书馆/scheduler.py#L100) 据此 `set_catalog_time()` | `exhausted` 实际含义是"本轮没抓满预算"，从不代表"翻到目录末尾"。实测场景：3 个 id 全已在库 → `exhausted=True` → 该 fonds 封存 7 天。而 `/fonds/3611449` 真实 **5268 件**、每页 100 件、每心跳预算 40 → 全量入库约 **370 周**（Lead 已用桩复现，见 §4.2） | 严格定义：`exhausted = 翻到最后一页 and 无遗漏 id`；新增 `reached_end` / `next_page`，把游标持久化到 `sources.catalog_cursor`，不靠"7 天过期" |
| **3** | **下载"成功"只比体积 → 半成品被永久判成功** | [http.py:163-164](file:///Users/sec-t/Documents/coding/八国联军图书馆/core/http.py#L163) skip-if-exists；[loc.py:236](file:///Users/sec-t/Documents/coding/八国联军图书馆/sites/loc.py#L236) `min_bytes=100_000`（≈整本 0.5%） | 不发任何请求直接 `return True`；真实库 `jobs.last_error` 里已有 `卷1: PDF 校验失败 Stream has ended unexpectedly`，证明半截文件**曾真实落盘**。`doctor` 只数目录里有没有 `.pdf`，照样报 ✅ | 已存在文件也要过页数/哈希校验（`pdfbuild.pdf_page_count` ≥ 1 且与台账一致），不一致就删掉重下；`/api/doctor` 放开 `verify_n` |
| **4** | **`full_size` 跨 attempt 泄漏 → 可拼出"长度对、内容错"的文件** | [http.py:167](file:///Users/sec-t/Documents/coding/八国联军图书馆/core/http.py#L167) 循环外初始化；[http.py:188](file:///Users/sec-t/Documents/coding/八国联军图书馆/core/http.py#L188) `"ab"` 追加且拿不到 `Content-Range` 时不重置 | 审查员实测：尝试 1 的 5000B 残片 + 尝试 2 的 4000B 无总长响应 = 9000B，恰等于旧 `full_size` → 判成功。对古籍＝不可发现的错版 | `full_size` 移到循环内每次重置；206 响应拿不到总长时**丢弃残片重来**而不是追加 |
| **5** | **na_jp 分块下载无事务语义** | [na_jp.py:377,400-443](file:///Users/sec-t/Documents/coding/八国联军图书馆/sites/na_jp.py#L400) `parts` 函数级、失败分支 `return 0, []` | ①卷1成功+卷2失败 → 声称"无产出"但磁盘已有孤儿 PDF，下次重试把它当"已完成卷"跳过；②残留块被下一卷合并 → **卷 2 的 PDF 里混着卷 1 的页** | `parts` 改每卷局部；卷 PDF 先写 `.part` 再 `os.replace`；失败分支 `break` 并照实上报已完成卷 |

### P1 — 重要（语义错误 / 可靠性隐患 / 安全）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 6 | **SSRF 实测可利用** | [app.py:698](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/app.py#L698) 只校验 `http(s)://` 前缀；scheduler 原样请求 | 可写入 `http://169.254.169.254/`，且 `requests` 默认跟随重定向 → 域名白名单也挡不住。附带"有回显 SSRF"（抓到的内容进审核页） |
| 7 | **无鉴权假设一行代码都没体现** | 全站；`0.0.0.0:8080` | 任何能访问 8080 的设备＝管理员。DESIGN.md 的"可选简单 Token"未实现。启动无告警 |
| 8 | **5 个接口收到非法 JSON 直接 500** | [app.py:322,346,372,393,479,524](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/app.py#L322) `await request.json()` 裸调用 | Lead 已实测：`/api/bookmarks` `/api/annotations` `/api/review` `/api/reading-tick` 全部 500 |
| 9 | **输入无上限** | `annotation.text`、`note`、`/read?p=`、`zoom` | 实测 10MB 文本可入库；`/read?p=99999999` 渲染出 `data-page="99999999"` |
| 10 | **弱成功语义：缺卷永不补齐** | [pipeline.py:160](file:///Users/sec-t/Documents/coding/八国联军图书馆/core/pipeline.py#L160) `ok=bool(outputs)`；[app.py:531](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/app.py#L531) retry 只收 failed/dead | Lead 已实测：多卷书只成功 1 卷 → `done` → 离开 queued 队列 → `/api/retry` 拒绝 → **缺卷无任何自动回头路径** |
| 11 | **`dead` 不是终态 + `attempt` 无上限** | [db.py:491](file:///Users/sec-t/Documents/coding/八国联军图书馆/core/db.py#L491) | 经 Web「重试」实测可跑 **25 次**、日志打出 `失败(25/5)`；每次失败都占该馆每小时配额，把能下成功的书挤出队列 |
| 12 | **读取器布局切换 CSS 全失效** | [reader.js:220](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/static/reader.js#L220) `setLayout` 不写 `root.dataset.layout` | `#reader[data-layout=…]` 的 15 行 CSS 只在刷新后生效。Lead 已 grep 复核：`dataset.theme` 有写、`dataset.layout` 零命中 |
| 13 | **`sepia` 主题被静默吞掉** | [app.py:115](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/app.py#L115) `THEMES={"light","night"}`，CSS 已写 sepia | 用户切换的三个主题里有一个永远存不住，刷新回 light |
| 14 | **HEALTHCHECK 恒真** | [app.py:755](file:///Users/sec-t/Documents/coding/八国联军图书馆/web/app.py#L755) `db` 字段只是 `os.path.exists()` | Lead 已实测：删掉 DB 后仍返回 `{"ok": true, "db": false}` 且 HTTP 200。叠加 `restart: unless-stopped` → 磁盘满/DB 锁死永远不报警 |
| 15 | **entrypoint 不转发信号** | [entrypoint.sh:31](file:///Users/sec-t/Documents/coding/八国联军图书馆/entrypoint.sh#L31) 后台 scheduler 无 trap | 实测 `kill -TERM` 后子进程 PPID 变 1 → `docker stop` 时 scheduler 成孤儿，与重启后的新容器**可能双写同一 /data**（正是上一轮刚修过的同类问题） |
| 16 | **`migrate-nas.sh` 的 `DEST` 无校验、无 dry-run** | [migrate-nas.sh](file:///Users/sec-t/Documents/coding/八国联军图书馆/migrate-nas.sh) | 实测 `DEST=/` → `tar -x -C /` 可写满 QNAP 系统分区；而 README 让用户 `curl -O main/…` 后直接 `sh` 执行这个以 root 跑的脚本 |
| 17 | **UI 可启用未注册的 `ndl_jp`** | [db.py:115](file:///Users/sec-t/Documents/coding/八国联军图书馆/core/db.py#L115) 种子 vs [sites/__init__.py](file:///Users/sec-t/Documents/coding/八国联军图书馆/sites/__init__.py) 注册表 | 启用后每心跳刷异常并**跳过 `reap_stuck()`**；上传该源快照 → `KeyError` 冒泡 → HTTP 500 |
| 18 | **心跳漂移 + 冷却态只在内存** | [scheduler.py:163](file:///Users/sec-t/Documents/coding/八国联军图书馆/scheduler.py#L163) `time.sleep` 在轮次之后；`blocked_until` 是局部 dict | 一轮可能跑数小时 → 实际"15 分钟心跳"名存实亡；重启即丢冷却态 → 立刻再撞限流 |
| 19 | **测试套件守不住数据正确性** | [tests/test_regressions.py](file:///Users/sec-t/Documents/coding/八国联军图书馆/tests/test_regressions.py) | 8 次变异 5 次全绿（删掉 item 详情合并、配额窗口改 1 分钟、硬链接恒非幂等都测不出）；`pytest tests/` **收集 0 个测试却 exit 0**（Lead 已复核） |
| 20 | **文档与实现 19 处不一致** | README / DESIGN / READER-DESIGN / 代码注释 | DESIGN.md:197 列的 5 个 API **全不存在**；DESIGN 仍写 APScheduler、htmx、可选 Token（代码 0 命中）；README「重试5次」「9组测试」均过时 |

### P2 — 优化（已在各分报告列全，此处只列主线）

- **代码组织**：`web/app.py` 761 行混了 HTML 路由 + JSON API + 后台线程 + 上传导入，建议拆 `routers/` + `services/`；`library.html` / `review.html` 筛选区 ~90% 重复，抽 partial。
- **性能（实测影响小，非当务之急）**：`facets()` 全表解析 5000 行 = 15ms；每次 `connect()` 0.31ms；4 进程 1600 次写 0.84s 零失败 —— **DB 层不是瓶颈，别过早优化**。
- **a11y 系统性缺失**：全站 0 个 `aria-*`，阅读器 9 个图标按钮里 5 个可访问名为空，`.mut` 对比度 3.97:1，`user-scalable=no`。
- **死代码/遗留**：`core/mounts.py` 的 `os`、`core/doctor.py` 的 `migration_commands`、`tools/loc_fill_details.py` 的 `re`、`official_pdf_by_cids.py` 的 `sys`/`html` 未使用；根目录 `digital_archives_to_pdf.py` / `official_pdf_by_cids.py` 能力已并入 `sites/na_jp.py`，且前者**模块级缺 `import json`**、用浏览器 UA 绕节流、6 线程并发（正是被封的元凶）→ 建议删除。
- **依赖未受控**：无 lock/hash；实测装最新允许版本（fastapi 0.162 / pypdf 6.21）回归仍全过、冒烟正常 —— **当前未坏，但不可复现**。

---

## 2. "框架是否需要优化"——直接回答

**结论：下载内核抽象得好，适配器框架缺"机制"，建议做一次小重构而不是大改。**

做得好的（不用动）：
- `sites/base.py` 的 `assemble_pages_to_pdf` / `write_meta_json` 把 IIIF 组图与台账沉淀成公共实现，新增一个 IIIF 馆的 download 只需 ~40 行 —— 这个抽象是对的。
- 配额下沉到 `jobs` 表、在 `BEGIN IMMEDIATE` 里原子消费；`reap_stuck` 的租约模型 —— 设计正确，测试也覆盖了。
- 路径段校验（`_dest_dir` + `commonpath`）实测全拦。

需要动的（按 ROI 排序）：
1. **能力声明 + 启动自检**：`SourceAdapter` 是 `Protocol` 却非 `runtime_checkable`、`Dict[str, Type[SourceAdapter]]` 静态无效 —— 实测 `loc` 没有 `harvest_step` 也照样能注册能跑。加一个启动时 `for sid, cls in REGISTRY.items(): assert hasattr(...)` 的自检，5 行就能拦住"注册了但不能用"。
2. **注册表成为唯一真相源**：现在 `sources` 表种子里有 `ndl_jp`，注册表里没有，于是有了 P0/P1-17。让 `_DEFAULT_SOURCES` 由 adapter 的 `META` 自动生成，杜绝"表里有、代码里没有"。
3. **`get_adapter` 三处统一捕获**：`seed_catalog` / `fetch_one` / `import_collection_payload` 对 `KeyError` 的处理不一致（一处静默、一处刷异常、一处 500）。
4. **新增一馆 9 处改动里，有 6 处是"漏了不报错"**：限流表、`COLLECTION_LABELS`、README/DESIGN 表、测试。前两者可以做成 adapter 自带属性 + 自动汇总。

---

## 3. 交叉验证记录（Lead 独立复核，不采信单一审查员）

| 结论 | 来源 | Lead 复核 | 结果 |
|---|---|---|---|
| `attempt` 每次 +2，5 次变 2.5 次 | Lead 初判 | 写脚本实测 | **驳回**（A 的判断正确）：`row` 是 claim 前快照，`+1` 恰等于 DB 当前值；真问题是 `dead` 非终态 + 无上限 |
| `dead` 非终态可无限重试 | A | 读代码 + API 语义 | ✅ 成立 |
| `exhausted` 语义错配 | C | 桩脚本复现 3 场景 | ✅ 成立（见 §4.2） |
| 缺卷不可补齐 | A + Lead | 端到端脚本：done → 离队 → retry 拒绝 | ✅ 成立 |
| `settings.html` 嵌套 form | B | grep 行号复核 | ✅ 成立（31 / 63 / 70 / 76） |
| `dataset.layout` 从不写 | B | grep `dataset.layout` 零命中 | ✅ 成立 |
| `sepia` 被吞 | B | 读 `THEMES` + CSS | ✅ 成立 |
| XSS 已收口 | B | 注入 `<img onerror>` / `<script>` 实测 | ✅ 成立（`\u003c` 转义、`&#39;` 转义均正确） |
| 5 个接口非法 JSON 500 | B | TestClient 实测 | ✅ 成立 |
| HEALTHCHECK 恒真 | D | 删 DB 后实测 | ✅ 成立（200 + `db:false`） |
| `pytest` 假绿灯 | D | `pytest tests/ -q` | ✅ 成立（no tests ran，exit 0） |
| na_jp 重试计数 5 次变 2.5 次 | C（P1 中一条） | 对照 A 的实测 | ❌ **C 这条措辞有误**，与 A 同源但 A 的结论正确，已按 A 口径统一 |

> 说明：C 的另一条 `KeyError`（[scheduler.py:102](file:///Users/sec-t/Documents/coding/八国联军图书馆/scheduler.py#L102)）实际被 `_source_round` 外层 `except Exception` 捕获，**不会**炸穿整轮心跳或跳过 `reap_stuck()`（只有 `BaseException` 才会）。其真实后果是"收割失败仍按成功记日志"，严重度应从 P0 降为 P1。

---

## 4. 关键证据附录

### 4.1 `exhausted` 语义实测（Lead，桩 http，零联网）

```
全部已在库   → {'ids': 3, 'fetched': 0, 'skipped': 3, 'blocked': False, 'exhausted': True}
             → scheduler 会 set_catalog_time()（封存 7 天）吗？True
3 个新条目 预算 2 → {'ids': 3, 'fetched': 2, 'skipped': 0, 'blocked': False, 'exhausted': False}
```

三种场景里 `exhausted` 只在"本轮没抓满预算"时为 True —— 桩里列表页永远只有这 3 个 id，**根本不存在"翻到底"的概念**。而真实目录有 5268 件。

### 4.2 弱成功语义实测（Lead）

```
claim → 适配器只产出 1 卷（2、3 卷失败）→ set_status(done)
status = done   attempt = 0   last_error = ''
队列里还有该书吗: False
/api/retry → {"error": "仅 failed/dead 可重试"}   ← 缺卷无补救路径
```

### 4.3 变异测试命中率（D，8 次变异）

| 变异 | 后果 | 测试 |
|---|---|---|
| 删掉 `importer` 合并 item 详情 | 官方 PDF 直链 + 96 页清单全丢 | 🟢 全绿 |
| 配额窗口 `hours=1 → minutes=1` | 每小时配额名存实亡 | 🟢 全绿 |
| `_title_link` 恒返回 "new" | 硬链接非幂等 | 🟢 全绿（还印着"✓ 幂等"） |
| `download()` 去掉 min_bytes 判定 | 错信半成品 | 🟢 全绿 |

**命中率 3/8。** 这解释了为什么近期要连续手修 XSS / 双写 / 截断残片 —— 那些 bug 全落在盲区里。

---

## 5. 建议的落地顺序（4 个批次，每批可独立验证）

**第 1 批 · 半天，止血**（3 行到 20 行级别，全部已验证）
1. `settings.html` 嵌套 form（用户改了不保存）
2. `THEMES` 加 `sepia`；`setLayout` 补 `root.dataset.layout = l`
3. 5 个 `await request.json()` 包 try → 400
4. `/api/health` 改为真探测 DB（`SELECT 1`）；给 `annotation.text` / `note` / `page` 加上限
5. `pytest` 假绿灯：CI 里显式跑 `python3 tests/test_regressions.py`，并 grep 断言 `全部 13 组`

**第 2 批 · 1–2 天，修数据正确性**（本轮最高价值）
6. `http.py`：`full_size` 移进循环 + skip-if-exists 过页数校验（P0-3/4）
7. `na_jp.py`：`exhausted` 严格语义 + `parts` 每卷局部 + 卷 PDF 原子写（P0-2/5）
8. `pipeline.py`：弱成功语义 —— 部分成功的书保持在 `queued` 或允许 `done` 书重入队补卷
9. `db.py`：`attempt` 上限下沉到 SQL，`dead` 需 `force` 才可再认领；`update_download_info` 不得覆盖已有非零 `page_count`
10. `sources.catalog_cursor` 持久化收割游标，替代"7 天过期"

**第 3 批 · 1 天，补护栏**（防止第 2 批修完又被改回去）
11. 测试迁到 pytest（保留现有断言），补上 D 报告 §9 的 CI 清单 + B 报告 §9 的 6 个零依赖模板/JS 结构测试（能锁住 §1 里 #1 #12 #13 三个功能 bug）
12. 把 D 报告的 5 个变异用例固化成"守卫测试"—— 即断言"删掉合并逻辑后测试必须红"
13. `get_adapter` 统一异常；启动自检注册表 vs sources 表
14. SSRF：`catalog_url` 加域名白名单 + 禁跟随跨域重定向

**第 4 批 · 持续，工程化**
15. entrypoint 加 trap 转发信号；compose 加资源限制
16. `migrate-nas.sh` 加 `DEST` 白名单 + `--dry-run`；README 改为 pin tag + sha256
17. CI 加 `pull_request` 触发、actions pin SHA、pyflakes 进 CI
18. 文档对齐 19 处不一致（尤其 DESIGN 的 API 清单与 APScheduler 描述）
19. `app.py` 拆分；删除两个根目录遗留脚本；a11y 补 `aria-*`

---

## 6. 各分报告导航

| 报告 | 行数 | 条数 | 最值得先读 |
|---|---|---|---|
| [A-core.md](A-core.md) | 908 | P0×3 / P1×12 / P2×10 | §1 P0-1～3、§4「逐条回应可疑点」 |
| [B-web.md](B-web.md) | 1416 | P0×0 / P1×10 / P2×31 | §0 TL;DR、§3.1–3.2、§9 可抄的测试 |
| [C-adapters.md](C-adapters.md) | 564 | P0×5 / P1×13 / P2×15 | §1 P0-1～5、§4「新增一馆要改 9 处」 |
| [D-testdeploy.md](D-testdeploy.md) | 1142 | P0×0 / P1×11 / P2×10 | §1.3 变异实验、§5.2 加固 diff、§9 CI 清单 |
