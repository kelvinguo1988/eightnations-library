# 批 3 修复报告：测试防护（pytest 化 / 守卫测试 / 变异验证 / CI 强制）

> 执行日期：2026-10-28
> 依据：`docs/review/D-testdeploy.md` §1.3（变异实验）、§9（CI 清单）、`docs/review/SUMMARY.md` §5 批 3
> 范围：`tests/`、`conftest.py`、`pytest.ini`、`.github/workflows/docker-publish.yml`、`README.md`（仅测试小节）
> **未 commit**；未改动业务代码（除下述 1 处真实漏洞修复，见 §4）

---

## 0. 一览

| # | 条目 | 修前 | 修后 |
|---|------|------|------|
| 1 | pytest 化（`test_regressions.py` → pytest） | 13 组手写 `ok()` 断言，无 fixture/无参数化 | **95 个用例**，3 个文件，全离线 |
| 2 | 守卫测试覆盖批 1/批 2 的新契约 | 无 | 全部契约有断言（见 §2） |
| 3 | 变异命中率（核心 KPI） | **3/8**（5 个逃逸） | **12/12** |
| 4 | CI 强制 | 只跑已删除的 `test_regressions.py`（**构建必挂**） | pytest + 用例数非 0 + pyflakes + 变异 |
| 5 | 假绿防护 | `pytest` 报 no tests ran 却 exit 0 | CI 解析 junit，用例数 0 即失败 |

---

## 1. pytest 化

### 1.1 为什么必须做

审查 D 的原始结论：`python3 -m pytest tests/ -q` **报 "no tests ran" 却 exit 0**。
CI 里 `run: python tests/test_regressions.py` 用的是自制的 `ok()` 函数，
任何一条断言被 `try/except` 吞掉、或文件被误删，CI 都照样绿。

### 1.2 结构

```
tests/
├── test_core_data.py     39 → 42 个用例（存储/下载/管线/适配器/调度）
├── test_structure.py     11 个用例（模板结构、XSS、导出、前端源码）
├── test_web.py           25 个用例（TestClient 端到端：页面/API/边界）
└── mutation_check.py     变异测试（非用例文件，pytest.ini 已排除收集）
conftest.py               fixture：db / data_dir / http_factory / web_client / write_pdf
pytest.ini                testpaths、markers（guard/web/structural）
```

`conftest.py` 的关键设计：`data_dir` 用 `tmp_path` 隔离，`EIGHTNATIONS_DATA`
指向临时目录，**绝不读写宿主 `data/`**；`http_factory` 注入 `FakeResp`/`SessionStub`，
**零联网**。

### 1.3 原 13 组 → 新用例的映射（无遗漏）

| 原分组 | 新用例 |
|---|---|
| 数据筛选 | `test_snapshot_import_and_filters` |
| 繁体转换 | `test_jp2t_traditional_chinese` |
| 分类富化 | `test_na_jp_category_enrichment` |
| na_jp 收割器 | `test_harvest_contract_keys_and_backward_compat` + 5 个 `test_exhausted_*` + `test_harvest_start_page_cursor` |
| BnF SRU | `test_bnf_sru_parse_fixture` |
| 挂载检测 | `test_mounts_and_migration_hint` |
| 流式 PDF | `test_build_pdf_atomic_on_bad_jpeg`、`test_build_pdf_page_count` |
| jobs 配额 | `test_quota_ledger_atomic_claim`、`test_quota_window_boundary` |
| 租约回收 | `test_reap_stuck_lease` |
| 路径校验 | `test_dest_dir_and_upsert_reject_traversal` |
| 下载完整性 | 6 个 `test_download_*` |
| PDF 原子写出 | `test_build_pdf_atomic_on_bad_jpeg` |
| 书名链接 | `test_title_links_idempotent_on_nonempty_library` |

**原第 82 行的红**：`harvest_step` 新增 `reached_end`/`next_page`/`failed`，
旧断言是"5 键全等"。已按批 2 §5.1.1 的实测（旧 5 键值一个都没变）改为
**子集相等 + 新键存在性/类型**断言，并在用例名里写明 `backward_compat`。

---

## 2. 批 1 / 批 2 新契约的守卫测试

批 1（Web 层）：

| 契约 | 守卫用例 |
|---|---|
| settings 零嵌套 form | `test_settings_page_has_no_nested_forms` |
| 所有 source 控件归属 `/settings` | `test_all_source_controls_belong_to_settings_form` |
| 上传 form 自带 source/files | `test_upload_form_carries_its_own_source_and_files` |
| reader.js 写 dataset.layout | `test_reader_js_writes_dataset_layout` |
| `?v=` 版本号已 bump | `test_reader_js_version_bumped_in_template` |
| 非法 JSON → 400 | `test_invalid_json_body_returns_400` |
| health 503 | `test_health_503_when_db_unavailable` |
| `/read?p=` 三层 clamp | 4 个 `test_read_page_*` |
| 输入上限 | `test_annotation_text_truncated_to_max` 等 |

批 2（数据正确性）：

| 契约 | 守卫用例 |
|---|---|
| `exhausted` 严格四条件 | 5 个 `test_exhausted_*` |
| `start_page` 游标 | `test_harvest_start_page_cursor` |
| 多卷命名全部阅读器可见 | `test_na_jp_volume_names_all_reader_visible` |
| 分块部分失败照实上报 | `test_na_jp_partial_failure_reports_completed_volumes` |
| 弱成功 → `done`+`needs_topup=1` | `test_partial_success_stays_in_queue_until_topped_up` |
| `ok` 但 0 页视为失败 | `test_ok_with_zero_pages_is_failure` |
| attempt 上限 | `test_attempt_limit_stops_claiming_and_retry_resets`、`test_attempt_cap_enforced_in_sql_not_only_in_pipeline` |
| `dead` 需 force | `test_dead_requires_force` |
| `catalog_cursor` | `test_catalog_cursor_roundtrip_and_reset` |
| `_norm_stats` 不抛不返回 None | `test_norm_stats_never_raises_and_never_returns_none_fields` |
| `MAX_ATTEMPTS` 单一定义 | `test_max_attempts_constant_exposed_from_db` |
| validator 拒坏 PDF | `test_download_validator_rejects_bad_pdf` 等 2 个 |

---

## 3. 变异测试（核心 KPI：3/8 → 12/12）

### 3.1 工具

`tests/mutation_check.py`：对业务代码做一处必然有害的小改动 → 跑全量 pytest →
**变红 = 变异被杀死**（防线真实存在）；**仍绿 = 逃逸**（该行为没有覆盖）。

关键实现细节（都是踩坑后加的）：

- **每次跑测试前清 `__pycache__`**。变异把 `.py` 改坏后若进程被中断，会留下
  **变异版 `.pyc`**；之后即使源码已复原，只要 mtime/size 匹配，解释器仍加载旧
  字节码 → 表现为"源码看着对、行为却是坏的"。开发本脚本时真实踩到：
  `record_partial` 源码写着 `needs_topup=1`，实测却写 0，排查了很久。
- **`finally` 逐字节复原 + md5 校验**，异常/中断也不留脏改动。
- **前置检查**：未变异时必须全绿，否则"红"不能归因于变异。

### 3.2 结果

```
✓ 前置检查：未变异时全绿

✓ KILLED   A1 206 拿不到总长时继续追加残片（拼出长度对内容错的文件）
✓ KILLED   A2 skip-if-exists 不校验内容
✓ KILLED   A3 416 无总长时接受任意残片
✓ KILLED   C2 attempt 上限从 SQL 移除（dead 无限重试）
✓ KILLED   C1 部分完成也清欠账标记（缺卷永不补齐）
✓ KILLED   B2 exhausted 退回宽松语义（没抓满预算就算完）
✓ KILLED   B3 多卷首卷命名为 cover.pdf（阅读器看不到卷首）
✓ KILLED   D 允许 '..' 通过路径段校验（路径穿越）
✓ KILLED   E 模板关闭自动转义（XSS 复活）
✓ KILLED   F Markdown 导出不转义正文（导出文件执行脚本）
✓ KILLED   G reader.js 不写 dataset.layout（布局持久化失效）
✓ KILLED   H /api/health DB 不可用仍返回 200

结果：12/12 个变异被杀死
```

### 3.3 过程中发现并补上的 3 个覆盖缺口

初版只有 10 个变异、7 个被杀死。逃逸的 3 个都是**真实覆盖缺口**，不是脚本 bug：

**① A1 `full_size` 跨轮泄漏 —— 原用例是"假绿"**
原用例 `test_download_full_size_not_leaked_across_attempts` **没传
`expected_bytes`**，于是把 `attempt_size` 挪到循环外是**语义等价**的
（`None` 挪到哪都是 `None`）→ 变异逃逸。补了
`test_download_full_size_leak_with_known_expected_bytes`，显式传
`expected_bytes=9000`，构造审查 A P0-2 的原始错位场景（5000 残片 + 4000 全量
= 9000，恰好等于旧总长）。

**② A3 416 无总长 —— 完全没有用例**
补 `test_download_416_without_expected_bytes_is_failure`：416 且未给
`expected_bytes` 时必须判失败、且不得产出目标文件。

**③ C2 attempt 上限 —— 端到端用例覆盖不到 SQL 层**
`test_attempt_limit_stops_claiming_and_retry_resets` 里 pipeline 会把状态置
`dead`，于是"SQL 里的 `attempt < MAX`"即使被删掉，还有 `status not in allowed`
挡着 → 变异逃逸。补 `test_attempt_cap_enforced_in_sql_not_only_in_pipeline`：
绕过 pipeline，手工构造 `status='failed'` + `attempt=MAX` 的行，只有 SQL 上限
能拦住；并断言 `attempt=MAX-1` 仍可认领（防上限写成 `>` 永远拦）。

### 3.4 一个诚实的说明

变异"杀死"证明的是**该行为有测试覆盖**，不证明测试写得完美。
本批新增的 3 个用例都是针对"原来的用例为什么漏掉"来设计的，不是为了凑命中率。

---

## 4. 顺手修掉的一个真实漏洞（不在批 3 条目内）

`test_export_markdown_escapes_annotation_text` 初版**是红的**，查明是**产品 bug**：

`/export/annotations/{id}.md` 把标注正文、书名、`item_url` 原样拼进 Markdown。
**Markdown 允许内联 HTML**，GitHub / VS Code 预览 / Obsidian 都会执行
`<img src=x onerror="alert(1)">` → 用户导出标注后打开就中招。

修法（`web/app.py` 新增 `_md_escape`）：转义 `&`→`<`→`>`（先 `&` 避免二次转义），
应用于 title / item_url / kind / 正文；顺带把多行正文逐行加 `> `，
否则后续行会脱离引用块（甚至被当成标题/代码块）。

> 这条属于批 1 的 `web/` 范围，但批 1 已收工且测试由我收尾，故由 Lead 直接修，
> 已记入批 1 报告的后续项。

---

## 5. CI 强制

`.github/workflows/docker-publish.yml` 的 `test` job（**build-push 的 needs**）：

1. `py_compile` 全仓（保留原步）
2. **`pyflakes`**（0 告警才过；已把仓库里残留的 5 条未用导入清干净）
3. **`pytest` + junit 解析**：用例数为 0 → `::error::` 且 exit 1
4. **`mutation_check.py`**：有逃逸 → exit 1

第 3 步的断言逻辑已本地双向验证：
- 真实套件：`collected=95 failed=0` → 通过
- 空测试目录：`collected=0` → **判红**（假绿被抓住）

---

## 6. 最终验证

```
$ python3 -m pytest tests/ -q
95 passed, 1 warning in 2.27s

$ python3 -m pyflakes core web sites tools scheduler.py manage.py tests conftest.py
（无输出，exit 0）

$ python3 tests/mutation_check.py
结果：12/12 个变异被杀死
```

`git diff --stat` 确认变异脚本**未留下任何脏改动**（所有业务文件与原状态一致）。

---

## 7. 遗留 / 建议

- **T1** 前端无真实浏览器验证（本机无 Playwright）：`reader.js` 的布局持久化只有
  静态断言 + 线上 JS 版本号 bump。建议后续加 Playwright 冒烟（可选）。
- **T2** 变异清单目前 12 条，覆盖 P0 与主要 P1。后续修 bug 时**建议同步加一条变异**
  （"这条修复将来被改回去，测试会不会红？"），否则防护会随时间退化。
- **T3** `core/doctor.py` 的 `needs_topup` 统计由 Lead 补上（见批 2 T5）。
- **T4** `tests/mutation_check.py` 依赖源码片段精确匹配；业务代码重构后可能报
  `STALE`，届时按提示更新片段即可（脚本会明确告知而不是静默跳过）。
