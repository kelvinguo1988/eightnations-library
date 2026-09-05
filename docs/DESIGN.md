# 八国联军图书馆 · 系统设计方案

> 版本 v0.3（2026-08-29 · 四项核心决策已对齐；M1 已跑通，新增重大发现见事实 #7）
> 目标：自动从各国国家级数字图书馆采集流散中国典籍的 PDF，统一入库、限速计划获取、
> 新书发现与人工筛选，并通过 Web 前端在 QNAP NAS 上浏览、检索与在线阅读。

---

## 0. 已验证的技术事实（2026-08-29 实测）

| # | 事实 | 对设计的影响 |
|---|------|--------------|
| 1 | `www.loc.gov`（元数据 JSON API，`?fo=json`）套 Cloudflare 盾：curl、curl_cffi(chrome 指纹)、Playwright 无头/有头 Chromium、真实 Edge 均返回 403/挑战页 | 元数据获取不能假设"纯 HTTP 可行"，采集层必须**策略可插拔**（直连 / Cookie / 浏览器 / 手动快照） |
| 2 | `tile.loc.gov`（图像服务）**不在盾内**：普通请求返回 LoC 自身 404 页而非 Cloudflare 挑战；社区（yt-dlp、LoC data-exploration 官方仓库）证实 `tile.loc.gov/storage-services/service/...` 文件 URL 可直接下载 | 占带宽 95% 以上的**图像下载可以长期无人值守直连**，浏览器/过盾只与元数据有关 |
| 3 | LoC 官方提供开放 JSON API（无需 key，官方仓库 LibraryOfCongress/data-exploration 用 requests 演示下载）；永樂大典资源 ID 形如 `asianyongle1403.2018421670`，条目页 `/resource/...`、著录页 `/item/...` | 从"干净出口 IP"访问时纯 HTTP 即可；当前网络环境被盾可能是 IP/代理因素，换出口后直连方案可复活 |
| 4 | 日本国立公文書館两个脚本已跑通：IIIF manifest→JP2 原生 JPEG→纯标准库组 PDF（`digital_archives_to_pdf.py`）；官方 contentDownload 分块 PDF→pypdf 合并（`official_pdf_by_cids.py`）。限流经验：~58 次连续请求触发临时封禁、退避重试 + 页级断点续传 | 这套"页级断点 + 退避 + PDF 组装"代码直接复用为**公共下载内核** |
| 5 | 本机：Python 3.9、playwright/curl_cffi/pypdf 已装；Docker 29.4.3 已装（守护进程未启动）；部署目标 QNAP NAS | NAS 容器保持"纯 Python 轻镜像"；浏览器内核方案仅作为可选侧车 |
| 6 | IDP 主站（英/俄/德/法/日各中心合并库）对脚本返回 403（Cloudflare）；意大利文化部域名对境外 IP 封锁（备忘录 2026-08 实测） | 后续扩展馆时，每个馆的"可达性"差异很大，适配器必须允许"半人工目录"模式 |
| 7 | **LoC 条目自带官方整本 PDF 直链**（`resources[].pdf` → `tile.loc.gov/storage-services/public/asian/<lccn>/<lccn>.pdf`，实测 200KB/页、96 页/18.6MB 一请求直达）；IIIF 路径支持 `/full/<w>,/` 任意宽度（1600px 实测 200）：**下载一本书 = 1 个 HTTP 请求**，逐页组图只作无 PDF 条目的兜底 | LoC 采集速度与存储估算大幅下调；质量档位改为 `auto/pdf/orig/mid/thumb`，`auto`=官方PDF优先 |
| 8 | 经 ZCode 内置浏览器点击 Cloudflare 复选框一次后，永樂大典集合快照（43 条）+ 条目详情全部抓取成功（fixture 已存 `fixtures/loc/`） | snapshot 策略验证可行：浏览器人工过盾一次 → 会话内全自动翻页/抓详情 |
| 11 | **BnF 法国馆实测（2026-08-30，严格限速逐项验证）**：catalogue.bnf.fr SRU 开放（`bib.digitized all "freeAccess"` 索引可筛 Gallica 自由访问文献，数字化中文语种文献 1,011 条，UNMARC/marcxchange-v2）；gallica.bnf.fr/iiif 的 manifest+图像对脚本开放；写本特藏目录 archivesetmanuscrits SRU 403（敦煌写卷本体暂不可自动发现）。候选未通：CUDL 搜索被 CloudFront 拦、哈佛 API 当前网络不可达、MDZ 接口路径不可证、BL-IDP Cloudflare、意大利地理封锁 | 新增 `sites/bnf.py`（默认停用）；法国馆自动收割与下载全部走开放接口 |
| 9 | 中国善本集合级快照对多数条目**只给页数整数**，无官方 PDF 直链（实测仅 51/2028 有）、无逐页清单；页标识形如 `service:asian:lcnclscd:<lccn>:1A000:00001a`，无法从代表图推导整本页 URL → **必须逐条抓 item 详情**（tools/loc_fill_details.py 半自动补齐，可续跑） | 1977 册下载需先补详情；补齐前明确报错停在 failed，不会误下 |
| 10 | 大 PDF 的 Range 续传若不校验总长会混拼损坏（实测一册"Stream has ended unexpectedly"） | core/http.py 下载完整性以 Content-Length / Content-Range 总长为准，200 全量响应覆盖残片；已实测修复重下成功（88 页校验通过） |

---

## 1. 总体架构

单容器为主（FastAPI 进程内含调度器与工作线程），数据落 NAS 共享目录：

```
QNAP NAS (Container Station / docker compose)
┌────────────────────────────────────────────────────────────┐
│  容器 eightnations                                         │
│                                                            │
│  ┌─────────────┐      ┌─────────────────────────────────┐  │
│  │ 调度器       │      │ Web 服务 (FastAPI + Jinja2)      │  │
│  │ APScheduler  │─────▶│  书库浏览/筛选/详情/PDF打开        │  │
│  │ · 每馆配额   │      │  新书审核(批准/忽略)              │  │
│  │ · 时段/抖动  │      │  任务面板(队列/进度/失败/日志)     │  │
│  └──────┬──────┘      └────────────▲────────────────────┘  │
│         ▼                          │                       │
│  ┌──────────────────────┐   ┌─────┴──────┐                │
│  │ 采集工作线程          │──▶│ SQLite     │                │
│  │  sites/loc.py        │   │ (WAL, 单文件)│                │
│  │  sites/na_jp.py      │   └────────────┘                │
│  │  sites/ndl_jp.py ... │                                 │
│  └──────────┬───────────┘                                 │
│             ▼ (挂载卷)                                      │
│  /data/books/<source>/<collection>/<item_id>/              │
│      book.pdf + cover.jpg + meta.json                      │
│  /data/snapshots/<source>/catalog_<date>.json              │
│  /data/db/library.db   /data/logs/                         │
└────────────────────────────────────────────────────────────┘
        ▲ 挂载: /share/Container/eightnations/data → 容器 /data
```

**职责切分（核心思想：元数据与图像解耦）**

- **目录层（harvest）**：便宜、低频（每周/手动），产出"目录快照"，负责发现新书 → 与库内 diff → 生成 `discovered` 记录。允许四种实现策略（见 §4）。
- **下载层（fetch）**：高频、长期无人值守，从 `queued` 取任务，页级断点续传，组 PDF 落盘。
- **展示层（web）**：只读 SQLite + 文件系统，不参与采集。

---

## 2. 数据模型（SQLite）

```sql
-- 馆藏来源
CREATE TABLE sources(
  id TEXT PRIMARY KEY,              -- 'loc' / 'na_jp' / 'ndl_jp' / 'bnf' ...
  name TEXT NOT NULL,               -- 美国国会图书馆
  country TEXT, flag TEXT,          -- 美国 / 🇺🇸
  adapter TEXT NOT NULL,            -- sites/<adapter>.py
  enabled INTEGER DEFAULT 0,        -- 是否允许下载
  hourly_quota INTEGER DEFAULT 10,  -- 每小时册数上限
  quality TEXT DEFAULT 'mid',       -- 图像档位 orig/mid/thumb
  meta_strategy TEXT DEFAULT 'snapshot'  -- direct/cookie/browser/snapshot
);

-- 书目（一行 = 一个数字化条目，可含多卷）
CREATE TABLE books(
  id INTEGER PRIMARY KEY,
  source_id TEXT REFERENCES sources(id),
  source_uid TEXT NOT NULL,         -- 馆内唯一 ID，如 loc:asianyongle1403.2018421670
  title TEXT, alt_title TEXT,       -- 拼音题名 / 中文原题（永樂大典: 卷…）
  author TEXT, era TEXT,            -- 朝代/时期（明），year_start, year_end
  year_start INT, year_end INT,
  language TEXT, item_url TEXT,     -- 原馆页面
  cover_path TEXT,
  volume_count INT, page_count INT, -- 抓到多少记多少，可为空
  rights TEXT,                      -- LoC "No known restrictions..."
  raw_json TEXT,                    -- 原始元数据整体保存，字段演进不丢
  status TEXT NOT NULL DEFAULT 'discovered',
    -- discovered → queued → running → done
    --          ↘ ignored（人工忽略）  failed（可重试，dead 次数上限）
  collection TEXT,                  -- 馆内子专藏，如 yongle-da-dian / chinese-rare-books
  added_at TEXT, decided_at TEXT, finished_at TEXT,
  UNIQUE(source_id, source_uid)
);

-- 下载任务/作业日志
CREATE TABLE jobs(
  id INTEGER PRIMARY KEY, book_id INT REFERENCES books(id),
  state TEXT,                       -- pending/running/done/failed
  attempt INT, last_error TEXT,
  pages_done INT, pages_total INT,
  bytes_done INT, started_at TEXT, finished_at TEXT
);

-- 事件日志（前端日志页 + 排障）
CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT, level TEXT,
                    source TEXT, book_id INT, message TEXT);
```

**文件布局**（NAS 固定目录，人工可读）：

```
books/loc/yongle-da-dian/asianyongle1403.2018421670/
    book.pdf          # 合成后的整册 PDF（多卷条目则 vol_01.pdf ...）
    cover.jpg         # 封面缩略图（网格展示用）
    meta.json         # 原始元数据 + 采集记录（页数、耗时、校验和）
snapshots/loc/catalog_2026-08-29.json   # 目录快照，新书 diff 依据
db/library.db   logs/app.log
```

> 文件夹用 `item_id` 而非中文书名命名，避免文件系统/编码/重名问题；展示名一律查库。

---

## 3. 采集器适配层（每馆一个文件）

```python
# sites/base.py —— 统一契约
class BookMeta(TypedDict): ...
class SourceAdapter(Protocol):
    id: str
    def harvest_catalog(self, **kw) -> Iterator[BookMeta]:
        """发现层：产出书目元数据（不下载图像）。可全量/增量。"""
    def download_item(self, meta, dest_dir, limiter) -> DownloadResult:
        """下载层：页级断点续传；返回页数/字节数/产出文件。"""
```

**公共下载内核 `core/`（从现有日本脚本提炼）**
- `fetch.py`：HTTP 客户端（UA/Referer 可配）、退避重试、每域令牌桶限速、页级断点（目标文件存在且达标即跳过）
- `pdfbuild.py`：现有纯标准库 DCTDecode 组 PDF（JPEG 无损嵌入）+ pypdf 合并/页数校验
- `limiter.py`：每源并发=1~2、每小时配额（默认 10 册）、随机抖动、可配活跃时段（如仅 01:00–08:00）

**首批适配器**

| 适配器 | 目录层策略 | 下载层 | 备注 |
|--------|-----------|--------|------|
| `loc.py` 美国国会图书馆 | snapshot / browser（见 §4） | **官方 PDF 直链优先**（resources[].pdf, 1请求/册），无 PDF 时 IIIF 逐页组图（orig/mid/thumb 分级） | ✅ M1 已跑通：永樂大典 41 册已入库，4 册验证通过 |
| `na_jp.py` 日本国立公文書館 | 直接 HTTP（现成） | 官方 contentDownload PDF / IIIF 兜底 | 迁移现有两脚本 |
| `ndl_jp.py` 日本国立国会图书馆 | 直接 HTTP | IIIF | dl.ndl.go.jp，M4+ |
| `bnf.py` / `idp.py` / `onb.py` … | 待调研 | 待调研 | Gallica、IDP（Cloudflare）、ÖNB，M6 起逐馆迭代 |

---

## 4. LoC 过盾：元数据获取的四种策略（可插拔，运行时切换）

LoC 目录变化很慢（专藏一年更新几次），**发现层低频 + 可人工兜底**是合理代价；图像层不受影响。

| 策略 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| `snapshot`（推荐默认） | 需要刷新目录时，在本机跑 `tools/loc_snapshot.py`（真实浏览器/人工过盾一次，脚本自动翻页拉全所有 `?fo=json` 页），产出 JSON 快照放入 `snapshots/loc/`；容器自动 diff 入库 | 最稳、零浏览器依赖进 NAS；快照即审计记录 | 刷新目录需人工一步 |
| `cookie` | 从日常浏览器导出 `cf_clearance` 填入配置，容器内 requests 直连 | 半自动 | Cookie 绑 IP/UA、有效期短，过期要重来 |
| `browser` | 可选侧车容器（Xvfb+Playwright），容器内定期自动过盾抓目录 | 全自动 | 镜像 +1GB、对网络环境要求高、可能长期失效 |
| `direct` | 纯 requests 直连 `?fo=json` | 最简单 | 当前出口 IP 被盾；若 NAS 配置干净代理出口则复活（LoC API 本身开放） |

> M1 阶段先手工拿到永樂大典的 collection JSON（43 条）作为 fixture，定稿字段映射；
> 此后运行期一律吃快照，不再碰 www.loc.gov。

---

## 5. 计划任务与限速

- **调度器**：APScheduler（进程内，免额外服务）
  - `下载心跳`：默认每 15 分钟触发一次（`EIGHTNATIONS_HEARTBEAT` 秒可调；拉长更保守）；从 `queued` 按来源轮询取任务；每源持有常驻 HourQuota 滑动窗口，"本小时已启动册数 ≥ hourly_quota"则跳过——每小时配额是真实的小时窗（v0.4 修复：此前每次心跳新建配额对象，实际变成"每心跳 N 册"）。
  - `目录巡检`：每周触发一次 `harvest_catalog`（snapshot 策略时提示"请刷新快照"）。
  - `失败重试`：指数退避，重试 ≥5 次转 `dead`，前端可手动重排。
- **礼貌性**：每源并发 1、页间随机延时 0.8–2s、诚实 UA（如 `eightnations-archiver/0.1 (personal study; contact=...)`）、仅抓公开领域（rights 字段校验）、夜间时段可选。

---

## 6. Web 前端（FastAPI + Jinja2 + htmx，无 Node 构建链）

延续 `index.html` 的暗色视觉，5 个页面：

1. **书库**：卡片网格（封面、书名、馆藏徽标、年代、卷/页数）；筛选条 = 来源 / 专藏 / 朝代 / 年代区间 / 关键字；分页。
2. **详情**：完整元数据、原馆链接、`/data/books/...` 路径、状态；"在线阅读"（浏览器原生 PDF，`<embed>`，支持 Range 请求的静态挂载）+ "下载 PDF"。
3. **新书审核**：`discovered` 列表，支持按标题/专藏筛选，批量"入库下载 / 忽略"；入库后进入 `queued`。
4. **任务面板**：进行中/队列/失败列表，每源小时配额与进度条、最近事件日志。
5. **设置**：每源开关、配额、时段、图像档位、meta_strategy。

接口：`/api/books`、`/api/books/{id}`、`/api/queue/approve|ignore`、`/api/jobs`、`/api/sources`、`/data/`（PDF/封面静态挂载）。
仅局域网访问 + 可选简单 Token；不引入用户系统。

---

## 7. Docker / QNAP 部署

```yaml
# docker-compose.yml（Container Station 直接导入）
services:
  app:
    build: .
    image: eightnations:latest
    restart: unless-stopped
    ports: ["8080:8080"]
    volumes:
      - /share/Container/eightnations/data:/data   # 按实际 NAS 共享路径调整
    environment:
      - TZ=Asia/Shanghai
```

- 镜像：`python:3.12-slim` + pypdf/apscheduler/fastapi/uvicorn（约 200MB，无浏览器）；
  x86_64 与 arm64 用 `docker buildx` 双架构构建（QNAP 两种机型都有）。
- 部署后健康检查 `GET /api/health`；日志写 `/data/logs/` 便于 NAS 端查看。
- 备份 = 备份 `library.db` + `meta.json`（图像本身可重下，不纳入备份策略）。

---

## 8. 存储估算（图像档位必须在开发前定案）

以 LoC 中国善本 2,032 条、平均 ~200 页/条（≈40 万页）估（2026-08-29 按 194KB/页 实测修正）：

| 档位 | 单页体积 | 全量估算 | 说明 |
|------|---------|---------|------|
| `auto`/`pdf` 官方 PDF 直链 | ~0.2 MB | **≈80 GB** | 首选档：1 请求/册，速度最快 |
| `orig` 原图（IIIF `pct:100.0` 组图） | 2–4 MB | ≈1–1.6 TB | 仅珍本白名单 |
| `mid` 1600px 长边（IIIF `full/1600,`） | 0.4–0.8 MB | ≈200–320 GB | 无官方 PDF 条目的默认 |
| `thumb` 1024px | ~0.2 MB | ≈100 GB | 只做"存在性归档" |

永樂大典 41 册官方 PDF 全套仅约 0.8 GB，可直接 `auto` 档全收。

---

## 9. 开发计划（里程碑）

| 里程碑 | 内容 | 验收标准 | 预估 |
|--------|------|----------|------|
| **M0**（已完成） | 八国调研页 + 日本两脚本 | — | ✅ |
| **M1 骨架 + LoC 首册跑通** | ✅ **2026-08-29 完成**：仓库重构（core/sites/tools + manage.py CLI）；数据模型；快照导入（永樂大典 41 册入库）；官方 PDF 直链下载 1 册（96 页/18.6MB/4.5s，页数校验、sha256、meta.json、封面全通过）；IIIF 组图兜底链路测试通过；fetch-next 配额队列验证（3 本批量） | ✅ 达成 | ✅ |
| **M2 LoC 全量流水线** | ✅ **核心完成（2026-08-29）**：`scheduler.py` 常驻守护（纯标准库循环替代 APScheduler——需求仅为"每 5min 心跳+滑动配额"，零依赖更稳）；管线抽到 `core/pipeline.py`（CLI 与守护共用）；滚动 1 小时滑动窗口配额（实测一轮 10 册全成）；失败重试/dead 状态机就绪；快照重复导入幂等更新（即 diff）。剩余：中国善本 2,032 条快照扩展 | 挂机自动跑完永樂大典剩余 27 册（约 3 小时）；断电重启自动续跑 | ✅/1 天 |
| **M3 Web v1** | ✅ **2026-08-29 完成**：FastAPI + Jinja2 五页（书库卡片网格+五维筛选 / 详情+内嵌 PDF 阅读器 / 新书审核批量批准·忽略 / 任务面板进度条+失败重试 / 设置每馆配额档位）；API（/api/review、/api/fetch、/api/retry、/api/stats、/api/health）；/data 静态挂载 PDF 与封面。四页浏览器目检通过 | 浏览器完成"筛选→批准→观察下载→在线阅读"闭环 | ✅ |
| **M4 日本迁移** | ✅ na_jp 适配器（fonds 实时收割 100 册入库 + 官方 contentDownload 分块下载实测 29 页/21MB/27s + IIIF 兜底组图）；CLI `import-na-jp`。⏳ ndl_jp 待接口验证 | 日本两馆同样走审核→计划→落盘流程 | na_jp ✅ |
| **M5 Docker 化 + QNAP 落地** | ✅ 文件就绪：Dockerfile（python:3.12-slim，无浏览器轻镜像）+ docker-compose.yml（/share/Container/eightnations/data 挂载）+ entrypoint.sh（调度守护+web 同容器）+ 健康检查 | ⏳ 待 NAS 上 build & 一键起（本机 Docker 守护进程未启动，无法预先构建） | 文件✅ |
| **M6 扩馆迭代** | BnF Gallica → ÖNB → IDP/柏林（Cloudflare 需专项）→ 意/俄（地理封锁，半人工目录） | 每馆一个迭代周期，复用审核/调度/展示 | 长期 |
| **M7 增强（可选）** | 封面墙美化、多卷合集页、中文 FTS 检索、OCR 全文、统计面板、导出书单 | — | 按需 |

**近期第一步（M1 开工前唯一前置）**：拿到永樂大典 collection JSON 真实样例 ——
在能过盾的浏览器环境打开 `https://www.loc.gov/collections/yongle-da-dian/?fo=json` 保存，或由我用 browser-use 驱动真实浏览器导出，作为字段映射 fixture。

---

## 10. 风险与对策

| 风险 | 对策 |
|------|------|
| www.loc.gov 盾导致目录无法自动刷新 | 元数据/图像解耦；snapshot 策略人工兜底；换代理出口后 `direct` 可复活 |
| 中国善本全量下载存储爆炸 | §8 图像档位默认 `mid`；白名单 `orig`；每源独立开关与配额 |
| 各馆可达性差异（IDP/意大利 403 或地理封锁） | 适配器允许半人工目录；文档记录每馆策略；不强求全自动 |
| 数月长跑中断（NAS 重启/断网） | 页级断点 + SQLite 状态机；重启后心跳自动续跑 |
| 站点改版/字段漂移 | 原始 JSON 全存 `raw_json`/`meta.json`，适配器只做薄映射，坏了快修 |
| 法律与礼貌 | 仅公开领域、限速、诚实 UA、记录 rights 与原馆链接 |

---

## 11. 已定决策（2026-08-29 对齐）

| 决策项 | 结论 |
|--------|------|
| 前端栈 | FastAPI + Jinja2 + htmx，单容器，无 Node 构建链，视觉沿用 index.html 暗色风格 |
| LoC 元数据策略 | 默认 `snapshot`（Mac 半自动导出快照 → NAS diff）；`direct/cookie/browser` 留作策略枚举，后续按需补实现 |
| 图像档位 | 分级：默认 `auto`（官方 PDF 直链优先），永樂大典等珍本白名单可走 `orig` 原图；按馆/按专藏可配 |
| 数据库 | SQLite（WAL），备份 = 拷文件 |
| 调度器 | APScheduler 进程内 |
| 部署 | 单容器 docker compose，QNAP Container Station，`/share/Container/eightnations/data` 挂载（路径以实际 NAS 为准） |

**当前进度**：M1 完成（详见 §9）；下一步 M2——APScheduler 接入 + 剩余 37 册挂机 + 中国善本快照扩展。
