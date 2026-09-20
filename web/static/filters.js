/* 书城 / 新书审核：筛选药丸的 URL 参数读写。
 * 由 library.html 与 review.html 共同引入（替代此前两页逐字重复的内联 <script>）。
 * 药丸不再使用 onclick 内联 JS（避免远端元数据值经属性解析还原引号后注入），
 * 改为 data-param / data-value / data-invert 属性 + 底部事件委托。 */
function toggleParam(key, val) {
  const p = new URLSearchParams(location.search);
  let cur = (p.get(key) || "").split(",").filter(Boolean);
  const i = cur.indexOf(val);
  if (i >= 0) cur.splice(i, 1); else cur.push(val);
  if (cur.length) p.set(key, cur.join(",")); else p.delete(key);
  p.delete("page");
  location.search = p.toString();
}
function invertParam(key, all) {
  const p = new URLSearchParams(location.search);
  const cur = (p.get(key) || "").split(",").filter(Boolean);
  const inv = all.filter((x) => !cur.includes(x));
  if (inv.length) p.set(key, inv.join(",")); else p.delete(key);
  p.delete("page");
  location.search = p.toString();
}
function clearParam(key) {
  const p = new URLSearchParams(location.search);
  p.delete(key);
  p.delete("page");
  location.search = p.toString();
}

/* 事件委托：
 *   .fpill[data-param][data-value]  → 增删该值（toggleParam）
 *   .fpill[data-param][data-invert] → 反选，data-invert 为 |tojson 序列化的全量数组
 *   .fpill.clear[data-param]        → 清空该参数（clearParam） */
document.addEventListener("click", (ev) => {
  const el = ev.target.closest(".fpill[data-param]");
  if (!el) return;
  const key = el.dataset.param;
  if (el.dataset.value !== undefined) {
    toggleParam(key, el.dataset.value);
  } else if (el.dataset.invert !== undefined) {
    let all = [];
    try { all = JSON.parse(el.dataset.invert); } catch (e) { return; }
    invertParam(key, all);
  } else if (el.classList.contains("clear")) {
    clearParam(key);
  }
});
