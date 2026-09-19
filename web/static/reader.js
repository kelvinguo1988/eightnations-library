/* 八国联军图书馆 · 阅读器（PDF.js v4 本地自托管）
 * 连滚 / 单页 两种布局；阅读进度自动记忆；页级书签；夜间滤镜；手机滑动翻页。 */
import * as pdfjsLib from "/static/pdfjs/pdf.min.mjs";

pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.min.mjs";

const $ = (id) => document.getElementById(id);
const root = $("reader");
const BOOK = +root.dataset.book;
const PDF_URL = root.dataset.pdf;
const TOTAL = +root.dataset.total || null;
let curPage = +root.dataset.page || 1;
let zoom = Math.min(Math.max(+root.dataset.zoom || 1, 0.5), 3);
let layout = root.dataset.layout === "single" ? "single" : "scroll";
let scrollPct = +root.dataset.scrollpct || 0;
let night = root.classList.contains("night");
let doc = null, pageCount = 0, renderTasks = new Map(), touchX = null;
let saveTimer = null, lastScrollY = 0;

const viewer = $("viewer"), pagesEl = $("pages");

/* ---------- 初始化 ---------- */
async function init() {
  doc = await pdfjsLib.getDocument({ url: PDF_URL }).promise;
  pageCount = doc.numPages;
  $("total-page").textContent = pageCount;
  if (!TOTAL || TOTAL !== pageCount) { /* meta.json 页数仅作预览，以实际为准 */ }
  buildPlaceholders();
  bindUI();
  applyLayout();
  // 恢复上次位置
  const target = Math.min(Math.max(curPage, 1), pageCount);
  goToPage(target, scrollPct, true);
  saveProgressSoon();
}

function fitWidth() {
  return Math.floor((viewer.clientWidth - 16) * zoom * (devicePixelRatio || 1));
}

function buildPlaceholders() {
  // 先取第 1 页宽高比做占位（渲染时校正）
  pagesEl.innerHTML = "";
  pagesEl.style.width = (viewer.clientWidth - 16) * zoom + "px";
  for (let i = 1; i <= pageCount; i++) {
    const div = document.createElement("div");
    div.className = "page";
    div.dataset.page = i;
    div.style.aspectRatio = "1 / 1.4";
    div.innerHTML = `<span class="pnum">${i}</span>`;
    pagesEl.appendChild(div);
  }
}

/* ---------- 渲染（可见页才渲染） ---------- */
const io = new IntersectionObserver((entries) => {
  for (const en of entries) {
    const p = +en.target.dataset.page;
    if (en.isIntersecting) renderPage(p);
    else { // 离屏释放内存
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
    const scale = fitWidth() / vp0.width;
    const vp = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(vp.width);
    canvas.height = Math.floor(vp.height);
    entry.canvas = canvas;
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
    if (renderTasks.get(p) !== entry) return;   // 已被释放/缩放变更
    holder.replaceChildren(canvas, Object.assign(document.createElement("span"),
      { className: "pnum", textContent: p }));
    entry.rendering = false;
  } catch (e) {
    if (String(e).includes("Rendering cancelled")) return;
    console.error("render", p, e);
    renderTasks.delete(p);
  }
}

/* ---------- 当前页判定 + 进度保存 ---------- */
function detectPage() {
  if (layout === "single") return curPage;
  const mid = viewer.scrollTop + viewer.clientHeight / 2;
  let best = 1, bestD = Infinity;
  for (const div of pagesEl.children) {
    const top = div.offsetTop, h = div.offsetHeight || 1;
    const d = Math.abs(top + h / 2 - mid);
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
  const body = JSON.stringify({ page: curPage, scroll_pct: pct, zoom,
                                layout, night });
  fetch(`/api/progress/${BOOK}`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body,
    keepalive: true });
}
addEventListener("pagehide", saveProgress);
addEventListener("beforeunload", saveProgress);

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
  if (layout === "scroll") {
    const target = pageTop(p) + (pagesEl.querySelector(`.page[data-page="${p}"]`)
                      ? (pagesEl.querySelector(`.page[data-page="${p}"]`).offsetHeight) * pct : 0);
    viewer.scrollTop = target;
  } else {
    renderPage(p); renderPage(p + 1); renderPage(p - 1);
  }
  updateStar();
  if (!force) saveProgressSoon();
}

viewer.addEventListener("scroll", () => {
  const p = detectPage();
  if (p !== curPage) { curPage = p; $("cur-page").textContent = p; $("page-jump").value = p; updateStar(); saveProgressSoon(); }
  lastScrollY = viewer.scrollTop;
}, { passive: true });

/* ---------- 布局 / 缩放 / 夜间 ---------- */
function applyLayout() {
  root.dataset.layout = layout;
  if (layout === "scroll") {
    pagesEl.style.width = (viewer.clientWidth - 16) * zoom + "px";
    buildPlaceholders();
    goToPage(curPage, 0, true);
    for (let p = curPage - 1; p <= curPage + 2; p++) if (p >= 1 && p <= pageCount) renderPage(p);
  } else {
    buildPlaceholders();
    goToPage(curPage, 0, true);
  }
}

function rerenderAll() {
  renderTasks.forEach((t) => { if (t.canvas) t.canvas.remove(); });
  renderTasks.clear();
  pagesEl.style.width = layout === "scroll" ? (viewer.clientWidth - 16) * zoom + "px" : "";
  for (const div of pagesEl.children) { div.style.aspectRatio = "1 / 1.4"; div.replaceChildren(); }
  applyLayout();
}

function setZoom(z) {
  zoom = Math.min(Math.max(z, 0.5), 3);
  $("zoom-val").textContent = Math.round(zoom * 100) + "%";
  rerenderAll();
}

function toggleNight() {
  night = !night;
  root.classList.toggle("night", night);
  saveProgressSoon();
}

function setLayout(l) {
  layout = l; $("layout-btn").textContent = l === "single" ? "📄" : "⇔";
  rerenderAll(); saveProgressSoon();
}

/* ---------- 书签 ---------- */
async function updateStar() {
  const list = await (await fetch(`/api/bookmarks?book_id=${BOOK}`)).json();
  const has = list.some((b) => b.page === curPage);
  $("bm-btn").textContent = has ? "⭐" : "☆";
  return list;
}

$("bm-btn").onclick = async () => {
  const list = await (await fetch(`/api/bookmarks?book_id=${BOOK}`)).json();
  const has = list.find((b) => b.page === curPage);
  if (has) { await fetch(`/api/bookmarks/${BOOK}/${curPage}`, { method: "DELETE" }); toast("已取消书签"); }
  else { await fetch("/api/bookmarks", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_id: BOOK, page: curPage }) }); toast("已加书签"); }
  refreshBookmarks();
};

async function refreshBookmarks() {
  const list = await updateStar();
  const ul = $("bm-list");
  ul.innerHTML = list.length
    ? list.map((b) => `<li data-page="${b.page}"><b>${b.page}</b> 页 ${b.note ? "· " + b.note : ""} <button class="bm-del" data-page="${b.page}">✕</button></li>`).join("")
    : `<li class="mut">暂无书签 —— 顶部 ⭐ 收藏当前页</li>`;
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

/* ---------- UI 绑定 ---------- */
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 1800);
}

function bindUI() {
  $("zoom-in").onclick = () => setZoom(zoom + 0.2);
  $("zoom-out").onclick = () => setZoom(zoom - 0.2);
  $("zoom-val").textContent = Math.round(zoom * 100) + "%";
  $("layout-btn").textContent = layout === "single" ? "📄" : "⇔";
  $("layout-btn").onclick = () => setLayout(layout === "single" ? "scroll" : "single");
  $("night-btn").onclick = toggleNight;
  $("toc-btn").onclick = () => { refreshBookmarks(); $("toc-drawer").hidden = !$("toc-drawer").hidden; };
  $("toc-close").onclick = () => ($("toc-drawer").hidden = true);
  $("prev-btn").onclick = () => goToPage(curPage - 1);
  $("next-btn").onclick = () => goToPage(curPage + 1);
  $("page-jump").onchange = () => goToPage(+$("page-jump").value || 1);
  addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") goToPage(curPage - 1);
    if (e.key === "ArrowRight" || e.key === "PageDown") goToPage(curPage + 1);
  });
  // 手机滑动翻页（单页模式）
  viewer.addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  viewer.addEventListener("touchend", (e) => {
    if (layout !== "single" || touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    if (Math.abs(dx) > 60) goToPage(curPage + (dx < 0 ? 1 : -1));
    touchX = null;
  }, { passive: true });
  // 双指缩放（单页模式）
  viewer.addEventListener("wheel", (e) => {
    if (e.ctrlKey) { e.preventDefault(); setZoom(zoom - Math.sign(e.deltaY) * 0.15); }
  }, { passive: false });
}

init();
