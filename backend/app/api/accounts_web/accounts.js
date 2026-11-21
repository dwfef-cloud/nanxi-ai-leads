/* 账号池管理页逻辑：多选 / 全选（当前筛选结果）/ 批量删除 / 状态复位。
   数据全部走 /accounts/api/*，不直接读写数据库。 */

const state = {
  items: [],
  selected: new Set(),
  keyword: "",
  only: "",
  allChecked: false,
};

const $ = (id) => document.getElementById(id);
const rowsEl = $("rows");
const selAll = $("selectAll");
const delBtn = $("delBtn");
const resetBtn = $("resetBtn");
const mask = $("mask");
const toastEl = $("toast");

/* ---------- 工具 ---------- */
function toast(text, isError) {
  toastEl.textContent = text;
  toastEl.className = "toast" + (isError ? " error" : "");
  toastEl.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.hidden = true; }, 2600);
}

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(String(value));
  if (Number.isNaN(d.getTime())) return String(value).slice(0, 16).replace("T", " ");
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------- 渲染 ---------- */
function render() {
  const list = state.items;
  if (!list.length) {
    rowsEl.innerHTML = '<tr><td colspan="10" class="empty">没有符合条件的账号</td></tr>';
  } else {
    rowsEl.innerHTML = list.map((it) => {
      const checked = state.selected.has(it.id) ? "checked" : "";
      const tags = [];
      if (it.isTest) tags.push('<span class="tag test">测试账号</span>');
      if (it.duplicate) tags.push('<span class="tag dup">重名</span>');
      if (it.limitStatus && it.limitStatus !== "healthy") {
        tags.push(`<span class="tag">限流 ${esc(it.limitStatus)}</span>`);
      }
      return `<tr class="${checked ? "selected" : ""}" data-id="${esc(it.id)}">
        <td><input type="checkbox" ${checked} data-role="pick" /></td>
        <td>${esc(it.name)}</td>
        <td>${esc(it.nickname) || "—"}</td>
        <td>${esc(it.platform)}</td>
        <td><span class="dot ${it.status === "online" ? "on" : "off"}"></span>${it.status === "online" ? "在线" : "离线"}</td>
        <td>${it.healthScore == null ? "—" : esc(it.healthScore)}</td>
        <td>${it.dailyOutreach == null ? "—" : esc(it.dailyOutreach)} / ${it.dailyLimit == null ? "—" : esc(it.dailyLimit)}</td>
        <td>${it.leadCount ? esc(it.leadCount) : "0"}</td>
        <td>${tags.join("") || '<span class="mono">—</span>'}</td>
        <td class="mono">${fmtDate(it.createdAt)}</td>
      </tr>`;
    }).join("");
  }
  $("sShown").textContent = list.length;
  $("selCount").textContent = state.selected.size;
  const n = state.selected.size;
  delBtn.disabled = n === 0;
  resetBtn.disabled = n === 0;
  selAll.checked = list.length > 0 && list.every((it) => state.selected.has(it.id));
  selAll.indeterminate = n > 0 && !selAll.checked;
  $("foot").textContent = `显示 ${list.length} / 共 ${state.allTotal ?? list.length} 个账号`;
}

/* ---------- 数据 ---------- */
async function load() {
  const q = new URLSearchParams({ keyword: state.keyword, only: state.only });
  try {
    const res = await fetch("/accounts/api/list?" + q.toString());
    const data = await res.json();
    if (!data || data.success !== true) throw new Error((data && data.error) || "读取失败");
    state.items = data.items || [];
    state.allTotal = data.summary ? data.summary.all : state.items.length;
    const ids = new Set(state.items.map((it) => it.id));
    state.selected = new Set([...state.selected].filter((id) => ids.has(id)));
    $("sAll").textContent = data.summary ? data.summary.all : "—";
    $("sDup").textContent = data.summary ? data.summary.duplicateAccounts : "—";
    $("sTest").textContent = data.summary ? data.summary.testAccounts : "—";
    render();
  } catch (err) {
    rowsEl.innerHTML = `<tr><td colspan="10" class="empty">读取失败：${esc(err.message)}</td></tr>`;
    toast("读取账号池失败：" + err.message, true);
  }
}

/* ---------- 交互 ---------- */
rowsEl.addEventListener("click", (e) => {
  const box = e.target.closest('input[data-role="pick"]');
  const row = e.target.closest("tr[data-id]");
  if (!row) return;
  const id = row.dataset.id;
  if (box || e.target.tagName === "TD") {
    if (box) {
      if (box.checked) state.selected.add(id); else state.selected.delete(id);
    } else if (!e.target.closest("input")) {
      // 整行点击也能选中，方便连续勾选
      if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
    }
    render();
  }
});

selAll.addEventListener("change", () => {
  if (selAll.checked) {
    state.items.forEach((it) => state.selected.add(it.id));
  } else {
    state.selected.clear();
  }
  render();
});

let kwTimer = null;
$("kw").addEventListener("input", (e) => {
  state.keyword = e.target.value.trim();
  clearTimeout(kwTimer);
  kwTimer = setTimeout(load, 220);
});

$("pills").addEventListener("click", (e) => {
  const btn = e.target.closest(".pill");
  if (!btn) return;
  [...document.querySelectorAll(".pill")].forEach((b) => b.classList.toggle("active", b === btn));
  state.only = btn.dataset.only || "";
  state.selected.clear();
  load();
});

/* ---------- 删除确认 ---------- */
let pendingAction = null;

function openConfirm({ title, body, okText, names, danger, onOk }) {
  $("mTitle").textContent = title;
  $("mBody").textContent = body;
  $("mOk").textContent = okText;
  $("mOk").className = danger ? "danger" : "";
  $("mList").innerHTML = (names || []).slice(0, 20)
    .map((n) => `<li>${esc(n)}</li>`).join("") +
    ((names || []).length > 20 ? `<li>… 其余 ${names.length - 20} 个</li>` : "");
  $("mList").hidden = !(names && names.length);
  mask.hidden = false;
  pendingAction = onOk;
}

function closeConfirm() {
  mask.hidden = true;
  pendingAction = null;
}

$("mCancel").addEventListener("click", closeConfirm);
mask.addEventListener("click", (e) => { if (e.target === mask) closeConfirm(); });
$("mOk").addEventListener("click", async () => {
  const fn = pendingAction;
  closeConfirm();
  if (fn) await fn();
});

delBtn.addEventListener("click", () => {
  const picked = state.items.filter((it) => state.selected.has(it.id));
  if (!picked.length) return;
  const totalLeads = picked.reduce((sum, it) => sum + (it.leadCount || 0), 0);
  openConfirm({
    title: `确认删除 ${picked.length} 个账号？`,
    body: `将同时清除这些账号的运行痕迹（事件/统计/私信记录）与本地登录态目录。`
      + (totalLeads ? `\n涉及历史线索 ${totalLeads} 条，线索本身会保留。` : "")
      + "\n此操作不可恢复，建议先确认没有正在运行的任务使用它们。",
    okText: "确认删除",
    danger: true,
    names: picked.map((it) => `${it.name}${it.nickname ? " / " + it.nickname : ""}`),
    onOk: async () => {
      try {
        const res = await fetch("/accounts/api/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: picked.map((it) => it.id) }),
        });
        const data = await res.json();
        if (!data || data.success !== true) throw new Error((data && data.error) || "删除失败");
        state.selected.clear();
        await load();
        toast(`已删除 ${data.removed} 个账号，历史线索保留`);
      } catch (err) {
        toast("删除失败：" + err.message, true);
      }
    },
  });
});

resetBtn.addEventListener("click", () => {
  const picked = state.items.filter((it) => state.selected.has(it.id));
  if (!picked.length) return;
  openConfirm({
    title: `复位 ${picked.length} 个账号的限流状态？`,
    body: "把这些账号的 limit_status 改回 healthy（不会改动健康分与日频上限）。",
    okText: "确认复位",
    danger: false,
    names: picked.map((it) => `${it.name}（当前 ${it.limitStatus}）`),
    onOk: async () => {
      try {
        const res = await fetch("/accounts/api/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: picked.map((it) => it.id) }),
        });
        const data = await res.json();
        if (!data || data.success !== true) throw new Error((data && data.error) || "复位失败");
        await load();
        toast(`已复位 ${data.updated} 个账号`);
      } catch (err) {
        toast("复位失败：" + err.message, true);
      }
    },
  });
});

/* ---------- 主题 ---------- */
const savedTheme = localStorage.getItem("accountsTheme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$("themeBtn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("accountsTheme", next);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !mask.hidden) closeConfirm();
  if (e.key === "a" && (e.ctrlKey || e.metaKey) && document.activeElement.tagName !== "INPUT") {
    e.preventDefault();
    selAll.checked = true;
    state.items.forEach((it) => state.selected.add(it.id));
    render();
  }
});

load();
