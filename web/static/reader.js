/* 八国联军图书馆 · 阅读器（PDF.js v4 本地自托管）
 * 连滚/双页/单页；进度记忆；书签；纸张主题；即时标注；阅读心跳。 */
import * as pdfjsLib from "/static/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.min.mjs";

const $ = (id) => document.getElementById(id);
const root = document.getElementById("reader");
const BOOK = +root.dataset.book;
const PDF_URL = root.dataset.pdf;
let curPage = +root.dataset.page || 1;
let zoom = Math.min(Math.max(+root.dataset.zoom || 1, 0.5), 3);
let layout = ["scroll", "double", "single"].includes(root.dataset.layout)
  ? root.dataset.layout : "scroll";
let theme = ["light", "sepia", "night"].includes(root.dataset.theme)
  ? root.dataset.theme : "light";
let scrollPct = +root.dataset.scrollpct || 0;
let doc = null, pageCount = 0, renderTasks = new Map(), touchX = null;
let saveTimer = null;
let annMode = false, anns = [], drawing = null, editTarget = null;

const viewer = $("viewer"), pagesEl = $("pages");
const COLORS = ["#ffe066", "#a5f3a1", "#7dcfff", "#f7768e"];
const KINDS = { highlight: "高亮", underline: "下划线", note: "备注" };

/* ---------- 初始化 ---------- */
async function init() {
  doc = await pdfjsLib.getDocument({ url: PDF_URL }).promise;
  pageCount = doc.numPages;
  $("total-page").textContent = pageCount;
  anns = JSON.parse(root.dataset.anns || "[]");
  buildPlaceholders();
  bindUI();
  applyTheme();
  goToPage(Math.min(Math.max(curPage, 1), pageCount), scrollPct, true);
  saveProgressSoon();
  setInterval(readingTick, 60000);          // 阅读心跳（每分钟）
}

function fitWidth() {
  const w = (viewer.clientWidth - 16) * (layout === "double" ? 0.48 : 1);
  return Math.floor(w * zoom * (devicePixelRatio || 1));
}

function buildPlaceholders() {
  pagesEl.innerHTML = "";
  pagesEl.style.width = layout === "double" ? "" : (viewer.clientWidth - 16) * zoom + "px";
  for (let i = 1; i <= pageCount; i++) {
    const div = document.createElement("div");
    div.className = "page";
    div.dataset.page = i;
    div.style.aspectRatio = "1 / 1.4";
    div.innerHTML = `<span class="pnum">${i}</span>`;
    pagesEl.appendChild(div);
  }
  renderAllOverlays();
}

/* ---------- 渲染（可见页才渲染，离屏释放） ---------- */
const io = new IntersectionObserver((entries) => {
  for (const en of entries) {
    const p = +en.target.dataset.page;
    if (en.isIntersecting) renderPage(p);
    else {
      const t = renderTasks.get(p);
      if (t && !t.rendering) { t.canvas.remove(); renderTasks.delete(p); }
    }
  }
}, { root: viewer, rootMargin: "300% 0px" });

async function renderPage(p) {
  if (renderTasks.has(p)) return;
  const holder = pagesEl.querySelector(`.page[data-page="${p}"]`);
  if (!holder) return;
  const entry = { rendering: true, canvas: null };
  renderTasks.set(p, entry);
  try {
    const page = await doc.getPage(p);
    if (renderTasks.get(p) !== entry) return;
    const vp0 = page.getViewport({ scale: 1 });
    holder.style.aspectRatio = `${vp0.width} / ${vp0.height}`;
    const scale = Math.max(fitWidth(), holder.clientWidth * (devicePixelRatio || 1)) / vp0.width;
    const vp = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    entry.canvas = canvas;
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
    if (renderTasks.get(p) !== entry) return;
    holder.replaceChildren(canvas, Object.assign(document.createElement("span"),
      { className: "pnum", textContent: p }));
    renderOverlays(p);
    entry.rendering = false;
  } catch (e) {
    if (String(e).includes("Rendering cancelled")) return;
    console.error("render", p, e);
    renderTasks.delete(p);
  }
}

/* ---------- 进度 ---------- */
function detectPage() {
  if (layout === "single") return curPage;
  const mid = viewer.scrollTop + viewer.clientHeight / 2;
  let best = 1, bestD = Infinity;
  for (const div of pagesEl.children) {
    const d = Math.abs(div.offsetTop + div.offsetHeight / 2 - mid);
    if (d < bestD) { bestD = d; best = +div.dataset.page; }
  }
  return best;
}

function saveProgressSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveProgress, 1200);
}

function saveProgress() {
  const pct = viewer.scrollHeight > viewer.clientHeight
    ? viewer.scrollTop / (viewer.scrollHeight - viewer.clientHeight || 1) : 0;
  fetch(`/api/progress/${BOOK}`, { method: "POST",
    headers: { "Content-Type": "application/json" }, keepalive: true,
    body: JSON.stringify({ page: curPage, scroll_pct: pct, zoom, layout, theme }) });
}
addEventListener("pagehide", saveProgress);
addEventListener("beforeunload", saveProgress);

function readingTick() {
  if (document.visibilityState !== "visible") return;
  fetch("/api/reading-tick", { method: "POST",
    headers: { "Content-Type": "application/json" }, keepalive: true,
    body: JSON.stringify({ book_id: BOOK, seconds: 60, page: curPage }) });
}

/* ---------- 导航 ---------- */
function pageTop(p) {
  const div = pagesEl.querySelector(`.page[data-page="${p}"]`);
  return div ? div.offsetTop - 8 : 0;
}

function goToPage(p, pct = 0, force = false) {
  p = Math.min(Math.max(p, 1), pageCount);
  curPage = p;
  $("cur-page").textContent = p;
  $("page-jump").value = p;
  if (layout !== "single") {
    const div = pagesEl.querySelector(`.page[data-page="${p}"]`);
    viewer.scrollTop = pageTop(p) + (div ? div.offsetHeight * pct : 0);
  } else {
    renderPage(p); renderPage(p + 1); renderPage(p - 1);
  }
  updateStar();
  if (!force) saveProgressSoon();
}

viewer.addEventListener("scroll", () => {
  const p = detectPage();
  if (p !== curPage) {
    curPage = p; $("cur-page").textContent = p; $("page-jump").value = p;
    updateStar(); saveProgressSoon();
  }
}, { passive: true });

/* ---------- 布局 / 缩放 / 主题 ---------- */
function applyTheme() {
  root.dataset.theme = theme;
  $("theme-btn").textContent = { light: "☀", sepia: "📄", night: "🌙" }[theme];
}

function rerenderAll() {
  renderTasks.forEach((t) => { if (t.canvas) t.canvas.remove(); });
  renderTasks.clear();
  buildPlaceholders();
  goToPage(curPage, 0, true);
}

function setLayout(l) {
  layout = l;
  $("layout-btn").textContent = { scroll: "⇔", double: "⧉", single: "📄" }[l];
  rerenderAll(); saveProgressSoon();
}

function setZoom(z) {
  zoom = Math.min(Math.max(z, 0.5), 3);
  $("zoom-val").textContent = Math.round(zoom * 100) + "%";
  rerenderAll();
}

/* ---------- 书签 ---------- */
async function updateStar() {
  const list = await (await fetch(`/api/bookmarks?book_id=${BOOK}`)).json();
  $("bm-btn").textContent = list.some((b) => b.page === curPage) ? "⭐" : "☆";
  return list;
}

async function refreshBookmarks() {
  const list = await updateStar();
  const ul = $("bm-list");
  ul.innerHTML = list.length
    ? list.map((b) => `<li data-page="${b.page}"><b>${b.page}</b> 页 ${b.note ? "· " + b.note : ""} <button class="bm-del" data-page="${b.page}">✕</button></li>`).join("")
    : `<li class="mut">暂无书签 —— 顶部 ☆ 收藏当前页</li>`;
  ul.querySelectorAll("li[data-page]").forEach((li) => {
    li.onclick = (e) => {
      if (e.target.classList.contains("bm-del")) return;
      $("toc-drawer").hidden = true; goToPage(+li.dataset.page);
    };
  });
  ul.querySelectorAll(".bm-del").forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      await fetch(`/api/bookmarks/${BOOK}/${b.dataset.page}`, { method: "DELETE" });
      refreshBookmarks();
    };
  });
}

/* ---------- 标注（即时模式） ---------- */
function pageRectNorm(e, holder) {
  const r = holder.getBoundingClientRect();
  return [Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1),
          Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1)];
}

function renderOverlays(page) {
  const holder = pagesEl.querySelector(`.page[data-page="${page}"]`);
  if (!holder) return;
  holder.querySelectorAll(".ann,.ann-editor").forEach((n) => n.remove());
  for (const a of anns.filter((x) => x.page === page)) {
    const d = document.createElement("div");
    d.className = "ann ann-" + a.kind;
    d.dataset.id = a.id;
    d.style.cssText = `left:${a.x0 * 100}%;top:${a.y0 * 100}%;` +
      `width:${(a.x1 - a.x0) * 100}%;height:${(a.y1 - a.y0) * 100}%;` +
      `--c:${a.color}`;
    if (a.kind === "note") d.textContent = "📝";
    holder.appendChild(d);
  }
}

function renderAllOverlays() {
  for (const div of pagesEl.children) renderOverlays(+div.dataset.page);
}

function openEditor(a, holder) {
  closeEditor();
  editTarget = a;
  const ed = document.createElement("div");
  ed.className = "ann-editor";
  ed.id = "ann-editor";
  ed.innerHTML = `
    <div class="swatches">${COLORS.map((c) =>
      `<button class="sw ${c === a.color ? "on" : ""}" data-c="${c}" style="--c:${c}"></button>`).join("")}</div>
    <div class="kinds">${Object.entries(KINDS).map(([k, v]) =>
      `<button class="kd ${k === a.kind ? "on" : ""}" data-k="${k}">${v}</button>`).join("")}</div>
    <textarea placeholder="备注 / 划词文字…">${a.text || ""}</textarea>
    <div class="ed-row"><button class="ed-save">保存</button><button class="ed-del">删除</button></div>`;
  holder.appendChild(ed);
  const top = Math.min(a.y1 * 100 + 2, 62);
  ed.style.top = top + "%";
  ed.style.left = Math.min(a.x0 * 100, 55) + "%";
  ed.querySelectorAll(".sw").forEach((b) => (b.onclick = () => { a.color = b.dataset.c; syncEdit(a, holder); }));
  ed.querySelectorAll(".kd").forEach((b) => (b.onclick = () => { a.kind = b.dataset.k; syncEdit(a, holder); }));
  ed.querySelector(".ed-save").onclick = async () => {
    a.text = ed.querySelector("textarea").value;
    await fetch(`/api/annotations/${a.id}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: a.kind, color: a.color, text: a.text }) });
    closeEditor(); renderOverlays(a.page); toast("已保存");
  };
  ed.querySelector(".ed-del").onclick = async () => {
    await fetch(`/api/annotations/${a.id}`, { method: "DELETE" });
    anns = anns.filter((x) => x.id !== a.id);
    closeEditor(); renderOverlays(a.page); toast("已删除");
  };
}

async function syncEdit(a, holder) {
  await fetch(`/api/annotations/${a.id}`, { method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: a.kind, color: a.color, text: a.text }) });
  openEditor(a, holder); renderOverlays(a.page);
}

function closeEditor() {
  root.querySelectorAll(".ann-editor").forEach((n) => n.remove());
  editTarget = null;
}

function setAnnMode(on) {
  annMode = on;
  $("ann-btn").classList.toggle("on", on);
  root.classList.toggle("annotating", on);
  closeEditor();
  if (!on) root.querySelectorAll(".ghost").forEach((n) => n.remove());
}

function bindAnnotation() {
  pagesEl.addEventListener("pointerdown", (e) => {
    const holder = e.target.closest(".page");
    if (!holder) return;
    if (e.target.closest(".ann-editor")) return;
    if (e.target.closest(".ann")) {           // 点击已有标注 → 编辑
      if (!annMode) {
        const a = anns.find((x) => x.id === +e.target.dataset.id);
        if (a) openEditor(a, holder);
        e.preventDefault();
      }
      return;
    }
    if (!annMode || drawing) return;
    e.preventDefault();
    const [x, y] = pageRectNorm(e, holder);
    drawing = { holder, page: +holder.dataset.page, x0: x, y0: y, x1: x, y1: y };
    const g = document.createElement("div");
    g.className = "ghost"; g.style.cssText = `left:${x * 100}%;top:${y * 100}%`;
    holder.appendChild(g);
  });
  pagesEl.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    e.preventDefault();
    const [x, y] = pageRectNorm(e, drawing.holder);
    drawing.x1 = x; drawing.y1 = y;
    const g = drawing.holder.querySelector(".ghost");
    if (g) g.style.cssText =
      `left:${Math.min(drawing.x0, x) * 100}%;top:${Math.min(drawing.y0, y) * 100}%;` +
      `width:${Math.abs(x - drawing.x0) * 100}%;height:${Math.abs(y - drawing.y0) * 100}%`;
  });
  pagesEl.addEventListener("pointerup", async (e) => {
    if (!drawing) return;
    const d = drawing; drawing = null;
    d.holder.querySelector(".ghost")?.remove();
    const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
    if (w < 0.02 || h < 0.01) return;
    const r = await fetch("/api/annotations", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ book_id: BOOK, page: d.page, kind: "highlight",
        x0: Math.min(d.x0, d.x1), y0: Math.min(d.y0, d.y1),
        x1: Math.max(d.x0, d.x1), y1: Math.max(d.y0, d.y1),
        color: COLORS[0] }) }).then((x) => x.json());
    if (r.ok) {
      anns.push({ id: r.id, book_id: BOOK, page: d.page, kind: "highlight",
        x0: Math.min(d.x0, d.x1), y0: Math.min(d.y0, d.y1),
        x1: Math.max(d.x0, d.x1), y1: Math.max(d.y0, d.y1),
        color: COLORS[0], text: "" });
      renderOverlays(d.page);
      openEditor(anns[anns.length - 1], d.holder);   // 即时：建完直接弹编辑
    }
  });
}

/* ---------- UI ---------- */
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 1800);
}

function bindUI() {
  $("zoom-in").onclick = () => setZoom(zoom + 0.2);
  $("zoom-out").onclick = () => setZoom(zoom - 0.2);
  $("zoom-val").textContent = Math.round(zoom * 100) + "%";
  $("layout-btn").textContent = { scroll: "⇔", double: "⧉", single: "📄" }[layout];
  $("layout-btn").onclick = () => {
    setLayout({ scroll: "double", double: "single", single: "scroll" }[layout]);
    toast({ scroll: "连滚", double: "双页", single: "单页" }[layout] + "布局");
  };
  $("theme-btn").onclick = () => {
    theme = { light: "sepia", sepia: "night", night: "light" }[theme];
    applyTheme(); saveProgressSoon();
  };
  $("ann-btn").onclick = () => { setAnnMode(!annMode); toast(annMode ? "🖍 标注模式：拖框即高亮" : "标注模式关闭"); };
  $("bm-btn").onclick = async () => {
    const list = await (await fetch(`/api/bookmarks?book_id=${BOOK}`)).json();
    const has = list.find((b) => b.page === curPage);
    if (has) { await fetch(`/api/bookmarks/${BOOK}/${curPage}`, { method: "DELETE" }); toast("已取消书签"); }
    else { await fetch("/api/bookmarks", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ book_id: BOOK, page: curPage }) }); toast("已加书签"); }
    refreshBookmarks();
  };
  $("toc-btn").onclick = () => { refreshBookmarks(); $("toc-drawer").hidden = !$("toc-drawer").hidden; };
  $("toc-close").onclick = () => ($("toc-drawer").hidden = true);
  $("prev-btn").onclick = () => goToPage(curPage - 1);
  $("next-btn").onclick = () => goToPage(curPage + 1);
  $("page-jump").onchange = () => goToPage(+$("page-jump").value || 1);
  addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") goToPage(curPage - 1);
    if (e.key === "ArrowRight" || e.key === "PageDown") goToPage(curPage + 1);
    if (e.key.toLowerCase() === "a") setAnnMode(!annMode);
  });
  viewer.addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  viewer.addEventListener("touchend", (e) => {
    if (layout !== "single" || annMode || touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 60) goToPage(curPage + (dx < 0 ? 1 : -1));
    touchX = null;
  }, { passive: true });
  viewer.addEventListener("wheel", (e) => {
    if (e.ctrlKey) { e.preventDefault(); setZoom(zoom - Math.sign(e.deltaY) * 0.15); }
  }, { passive: false });
  bindAnnotation();
}

init();
