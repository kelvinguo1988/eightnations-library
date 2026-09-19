# 书城 · 阅读体验设计（v1，待确认后开发）

> 目标：从"归档系统"升级为"书城 + 阅读器"——对标微信读书/书篱类应用的阅读体验，
> 适配电脑/手机/平板，支持 书架、标注、书签、划词搜索、划线高亮、阅读记忆。
> 本设计基于一个**关键实测事实**：现有 PDF 均为纯扫描图像、无 OCR 文本层
> （2026-09-19 用 pypdf 抽取 LoC/na_jp 样本均 0 字符）——所有交互据此设计。

---

## 1. 信息架构（页面地图）

```
书架 /                ← 新首页（默认落地页）
├── 继续阅读 大卡（上次读的书的封面+进度条+「第 N 页 · x%」+ 继续按钮）
├── Tab: 在读 / 已读完 / 收藏 / 全部
└── 长按(或右键)卡片: 加入收藏 · 标记读完 · 移出书架

阅读器 /read/{book_id}   ← 核心新页面（自定义 PDF.js 阅读器）
├── 顶部: 返回书架 · 书名 · 页码/总页 · 书签⭐ · 更多⋯
├── 主体: PDF.js canvas 渲染（滚动连页，移动端支持横滑翻页）
├── 底部工具条: 目录(书签列表) · 缩略图 · 检索 · 亮度/夜间 · 单双页 · 缩放 · 标注笔
└── 划选弹出菜单: 框选标注(高亮/下划线/备注) · 划词OCR搜索 · 复制

书城 /explore          ← 原「书库」页改造（筛选增强保留 + 分类聚合精选位）
详情 /books/{id}       ← 保留（元数据/下载/阅读入口）
任务 /jobs · 设置 /settings ← 保留
```

## 2. 阅读器（核心）

**内核**：PDF.js v4 本地自托管（`web/static/pdfjs/`，1.7MB 已就位，Apache-2.0）。
不原生 `<embed>`——只有自绘 canvas 才能叠加标注层和手势。

**功能矩阵**（按设备）：

| 功能 | 电脑 | 平板 | 手机 |
|---|---|---|---|
| 翻页 | 滚轮/方向键/PgUp·Dn | 同左+双指滑动 | **左右滑动翻页**、上下滚动可切 |
| 布局 | 单页/双页/连滚 | 单页/双页 | 单页/连滚 |
| 缩放 | 按钮/Ctrl+滚轮 | 双指捏合 | 双指捏合 |
| 夜间模式 | 滤镜反色（护眼，扫描件友好） | ✓ | ✓ |
| 标注 | 鼠标拖框 | 手指拖框 | 手指拖框（放大镜辅助） |
| 工具栏 | 顶部 | 底部 | 底部抽屉 |

**阅读记忆**（自动，无感）：
`reading_progress` 表存 每书: 页码、页内滚动比例、缩放、布局、夜间模式、时间戳。
打开即恢复到"上次那一眼"；书架卡片显示进度条与页码。

**书签**：页级书签 + 可选备注；书签列表即"目录"抽屉，点击跳转。

**标注/划线高亮**（扫描件方案：**矩形区域锚定页面坐标**，缩放无关）：
- 工具条选"高亮/下划线/备注"→ 在页面上拖出矩形 → 存
  `{页码, 归一化坐标 x0/y0/x1/y1, 颜色, 类型, 备注}`（下划线=压扁的高亮矩形）
- 叠加层是独立 canvas/absolute div，随 PDF.js 渲染同步重绘；
- 点击已有标注 → 编辑/换色/删除；
- 导出：详情页可导出该书全部标注为 Markdown（页码+截图+备注）。

**划词搜索（划词 OCR）**——扫描件的诚实解法：
- 划选区域 → 后端对该区域**按需 OCR**（Tesseract chi_sim+chi_tra，容器 +约 60MB）→
  返回文字 → 弹层显示，可**手动纠错** → 一键跳转外部检索（汉典 zdic / Google Books /
  原馆站内）或复制；OCR 结果可存为该区域的"文字备注"；
- 未来接入带文本层的 PDF（如 BnF 部分产品）时，PDF.js 文本层自动接管：
  真划词、真高亮，无需 OCR；
- **全书检索**（可选渐进）：对指定页范围后台批量 OCR（带进度、结果存 SQLite
  FTS5，一本书约 3-5 分钟/100 页，只跑一次），之后页码级全文搜索秒回。

## 3. 书城 / 书架

- **书架**是默认首页：继续阅读大卡（封面+进度+上次位置）> 在读 > 已读完 > 收藏；
  数据来源 = `reading_progress` + `books.status='done'` + 收藏标记；
- **书城**(/explore)：现书库筛选（馆/专藏/朝代多选/分类多选/关键字）保留，
  顶部增加"分类聚合精选"横滑卡（按 分类标签 聚合：漢籍·明刊本·佛教·方志…）；
- 书架与书城共享封面网格组件；书架卡片带进度条与"继续阅读"按钮。

## 4. 数据模型（新增 3 表 + 1 字段）

```sql
CREATE TABLE reading_progress(
  book_id INTEGER PRIMARY KEY REFERENCES books(id),
  page INTEGER, scroll_pct REAL, zoom REAL DEFAULT 1,
  layout TEXT DEFAULT 'scroll',           -- scroll/single/double
  night INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE bookmarks(
  id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id),
  page INTEGER NOT NULL, note TEXT DEFAULT '', created_at TEXT,
  UNIQUE(book_id, page)
);
CREATE TABLE annotations(
  id INTEGER PRIMARY KEY, book_id INTEGER REFERENCES books(id),
  page INTEGER NOT NULL,
  kind TEXT NOT NULL,                     -- highlight / underline / note
  x0 REAL, y0 REAL, x1 REAL, y1 REAL,     -- 归一化页面坐标
  color TEXT DEFAULT '#ffe066', text TEXT DEFAULT '',  -- OCR/备注文字
  created_at TEXT
);
ALTER TABLE books ADD COLUMN favorite INTEGER DEFAULT 0;
```

## 5. API

```
GET  /read/{book_id}                 阅读器页面（PDF.js shell）
GET  /data/.../book.pdf              既有静态挂载（Range 支持由 uvicorn 提供）
GET/PUT /api/progress/{book_id}      读/写阅读进度（PUT 用 sendBeacon 离页兜底）
GET/POST/DELETE /api/bookmarks       书签 CRUD（POST {book_id,page,note}）
GET/POST/DELETE /api/annotations     标注 CRUD（同上 + 坐标/颜色/文字）
POST /api/ocr                        {book_id, page, x0,y0,x1,y1} → 区域 OCR 文字
GET  /api/search/{book_id}?q=        书内检索（FTS5，命中页码列表）
POST /api/search-index/{book_id}     启动某书后台 OCR 建索引（带进度查询）
GET/POST /api/favorite/{book_id}     收藏切换
```

## 6. 页面布局示意（手机阅读器）

```
┌──────────────────────────┐        ┌──────────────────────────┐
│ ‹ 书架   永樂大典 卷6831  ⭐ ⋯ │        │  （PDF 页面 canvas）       │
├──────────────────────────┤        │   ▢←划选高亮区域(黄色半透明) │
│                          │        │                          │
│    （PDF 页面 canvas）     │   ⇄    │                          │
│    （左右滑动翻页）          │        │  [划选弹层]               │
│                          │        │  🖍高亮 〰下划线 📝备注      │
│                          │        │  🔍OCR搜索 📋复制 ✕        │
├──────────────────────────┤        ├──────────────────────────┤
│ 📖 目录 🔍 检索 ☀ 夜间 ⤢ 双页 12/96│  │ ⭐书签 🔍检索 🖍标注 ☀夜间 23/96 │
└──────────────────────────┘        └──────────────────────────┘
```

## 7. 里程碑

| 阶段 | 内容 | 预估 |
|---|---|---|
| **R1 阅读器 MVP** | PDF.js 阅读器页（连滚/单双页/缩放/夜间）+ 阅读进度记忆 + 书签 + 手机滑动翻页 | 1-1.5 天 |
| **R2 书架+书城** | 书架首页（继续阅读/收藏/进度条）+ 书城改造 + 响应式收尾 | 1 天 |
| **R3 标注系统** | 框选高亮/下划线/备注 + 叠加层渲染 + 标注管理 + Markdown 导出 | 1-1.5 天 |
| **R4 划词 OCR + 检索** | 区域 OCR 划词弹层 + 外部检索跳转 + 可选全书 FTS 索引 | 1-1.5 天 |

## 8. 已确认的技术事实

1. 现有 PDF 无文本层（pypdf 实测 0 字符）→ 文本交互全部走"区域坐标 + 按需 OCR"路线；
2. PDF.js v4 已本地化至 `web/static/pdfjs/`（1.7MB），无 CDN 依赖，NAS 内网可用；
3. LoC PDF 有官方整本直链 + 页数已知，阅读器可直接按页渲染；
4. uvicorn StaticFiles 支持 Range，PDF.js 渐进加载大 PDF 没问题。

## 9. Readest 功能借鉴评估（2026-09-19 调研 github.com/readest/readest）

| Readest 功能 | 评估 | 采纳方式 |
|---|---|---|
| 高亮/书签/备注 + 即时模式 | ✅ 借鉴交互 | R3：拖完选区直接默认色高亮（即时），弹层再调色/加备注，不打断阅读 |
| 书内/书库级检索 | ✅ 借鉴 | R4：FTS 单书检索扩展为**跨书检索**（对已建索引的书全局搜，结果侧栏列页码+片段） |
| 查词/词典弹层（维基百科等） | ✅ 借鉴 | R4：OCR 出字后弹层内一键 **汉典 / 维基百科 / Google Books** 查询（新窗口），替代单纯外链 |
| 主题色（夜间/纸张） | ✅ 借鉴 | R3：阅读器加"纸张主题"—— 白 / 米黄（纸张感）/ 夜间反色，扫描件用滤镜实现 |
| 阅读统计（时长/页数） | ✅ 借鉴 | R3.5：progress 表累计阅读秒数（前端心跳），书架显示"本周阅读 X 页 / Y 分钟" |
| TTS 朗读 | ⚡ 变通采纳 | 扫描件无文本层，但在 R4 全书 OCR 索引建成后，可用浏览器自带 speechSynthesis **朗读当前页 OCR 文本**（零依赖，中文质量一般） |
| 双页 Parallel Read | ⚡ 部分 | 平板/桌面**双页布局**补进 R3（R1 已实现单页/连滚）；双书分屏不采纳（小众） |
| 进度/书签同步 | — 不需要 | 单用户单 NAS，服务端存储天然"同步" |
| OPDS/Calibre、网页剪藏、EPUB/MOBI 导入、KOReader 同步 | ❌ 不采纳 | 与"图书馆扫描件归档阅读"定位不符 |
| TTS 媒体.overlay / 有声书、AI 摘要（building）、手写批注（planned） | ❌ 暂缓 | 扫描件无结构化文本/成本高，列入远期 backlog |

**结论**：R3/R4 范围更新为——
R3 标注（即时模式+纸张主题+双页布局+阅读统计）；
R4 划词 OCR（OCR 弹层内嵌查词跳转 + 跨书 FTS 检索 + 可选朗读）。

## 10. 风险

- 古籍刻本 OCR 识别率中等（60-85%），设计上让用户**可纠错**后再跳外部检索；
- Tesseract 增加镜像约 60MB（仅 R4 需要，不影响 R1-R3 提前发布）；
- 移动端手势与标注框选的冲突（拖框模式下禁用翻页滑动）——用工具条模式切换解决。
