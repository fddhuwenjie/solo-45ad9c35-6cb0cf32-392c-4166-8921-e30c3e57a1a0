/* 舞台快速换装编排 —— 原生 JS + SVG 联动 */
"use strict";

const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
let S = null;            // 完整状态
let selectedTask = null; // 选中任务 id
let drag = null;
let R = null;            // 选中连排 {run, plan, events, analysis}
let selectedRun = null;  // 选中连排 id

const TL = { left: 90, right: 1160, top: 34, laneH: 40, width: 1180 };
const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

async function api(url, method = "GET", body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
}

async function refresh(state) {
  S = state || (await api("/api/state"));
  renderAll();
}

/* ---------------- 时间轴 ---------------- */

function totalTime() {
  return Math.max(600, ...S.scenes.map((s) => s.start_sec + s.duration_sec)) + 60;
}
const xOf = (t) => TL.left + (t / totalTime()) * (TL.right - TL.left);
const tOf = (x) => ((x - TL.left) / (TL.right - TL.left)) * totalTime();

function el(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function txt(parent, x, y, str, attrs = {}) {
  const t = el("text", { x, y, "font-size": 10, ...attrs }, parent);
  t.textContent = str;
  return t;
}

function renderTimeline() {
  const svg = $("#timeline");
  svg.innerHTML = "";
  const laneOf = {};
  S.actors.forEach((a, i) => (laneOf[a.id] = i));
  const laneH = R ? 46 : TL.laneH;   // 选中连排时泳道加高，同轴叠放实测
  const h = TL.top + S.actors.length * laneH + 24;
  svg.setAttribute("height", h);

  // 场次条与刻度
  S.scenes.forEach((s) => {
    el("rect", { x: xOf(s.start_sec), y: 14, width: (s.duration_sec / totalTime()) * (TL.right - TL.left),
                 height: 14, fill: "#e8eef7", rx: 2 }, svg);
    txt(svg, xOf(s.start_sec) + 3, 24, s.name, { fill: "#456" });
  });
  for (let t = 0; t <= totalTime(); t += 300) {
    el("line", { x1: xOf(t), y1: TL.top - 4, x2: xOf(t), y2: h - 20, stroke: "#f0f0f0" }, svg);
    txt(svg, xOf(t) - 10, h - 8, fmt(t), { fill: "#aaa" });
  }
  // 演员泳道
  S.actors.forEach((a, i) => {
    const y = TL.top + i * laneH;
    txt(svg, 8, y + laneH / 2 + 4, a.name, { "font-size": 12, fill: "#333" });
    el("line", { x1: TL.left, y1: y + laneH, x2: TL.right, y2: y + laneH, stroke: "#eee" }, svg);
  });

  const W = S.schedule.windows;
  // 任务块（计划）
  S.tasks.forEach((t) => {
    const w = W[t.id];
    if (!w) return;
    const y = TL.top + laneOf[t.actor_id] * laneH + (R ? 4 : 7);
    const bh = R ? 13 : 26;
    const g = el("g", { class: "tblock" + (t.locked ? " locked" : "") +
                        (t.id === selectedTask ? " selected" : ""), "data-id": t.id }, svg);
    const color = !w.ok ? "#c0392b" : t.locked ? "#7f8c8d" : t.needs_review ? "#e67e22" : "#2980b9";
    el("rect", { x: xOf(w.start), y, width: Math.max(6, xOf(w.end) - xOf(w.start)),
                 height: bh, rx: 4, fill: color, opacity: 0.9 }, g);
    txt(g, xOf(w.start) + 4, y + (R ? 10 : 16), `#${t.id}${t.locked ? "🔒" : ""}`, { fill: "#fff" });
    // 截止线
    el("line", { x1: xOf(w.deadline), y1: y - 3, x2: xOf(w.deadline), y2: y + bh + 3,
                 stroke: "#c0392b", "stroke-dasharray": "3 2" }, g);
    g.addEventListener("pointerdown", (ev) => startDrag(ev, t));
    g.addEventListener("click", () => { selectedTask = t.id; renderAll(); });
  });
  // 实测条（选中连排时叠放）
  if (R) {
    const ivs = R.analysis.task_intervals;
    S.tasks.forEach((t) => {
      const w = W[t.id];
      if (!w) return;
      const y = TL.top + laneOf[t.actor_id] * laneH + 22;
      const iv = ivs[String(t.id)];
      if (iv) {
        const late = iv[1] > w.end + 5;
        el("rect", { x: xOf(iv[0]), y, width: Math.max(5, xOf(iv[1]) - xOf(iv[0])),
                     height: 13, rx: 3, fill: late ? "#c0392b" : "#27ae60", opacity: 0.9 }, svg);
        txt(svg, xOf(iv[0]) + 3, y + 10, "实测", { fill: "#fff", "font-size": 9 });
      } else {
        txt(svg, xOf(w.start) + 3, y + 10, "未打点", { fill: "#bbb", "font-size": 9 });
      }
    });
    // 异常打点（有效、未被补正）
    const superseded = new Set(R.events.filter((e) => e.supersedes).map((e) => e.supersedes));
    R.events.filter((e) => e.kind === "exception" && !superseded.has(e.id)).forEach((e) => {
      const t = S.tasks.find((x) => x.id === e.task_id);
      if (!t) return;
      const y = TL.top + laneOf[t.actor_id] * laneH;
      el("path", { d: `M${xOf(e.at_sec)},${y + 40} l4,-7 l4,7 z`, fill: "#e74c3c" }, svg);
      txt(svg, xOf(e.at_sec) + 6, y + 40, e.reason.slice(0, 14), { fill: "#c0392b", "font-size": 9 });
    });
    // 首个偏差
    const fd = R.analysis.first_deviation;
    if (fd) {
      const t = S.tasks.find((x) => x.id === fd.task_id);
      if (t) {
        const y = TL.top + laneOf[t.actor_id] * laneH;
        el("path", { d: `M${xOf(fd.time)},${y - 2} l5,8 l-10,0 z`, fill: "#e67e22" }, svg);
        txt(svg, xOf(fd.time) + 6, y + 2, `首个偏差 #${fd.task_id}`, { fill: "#d35400", "font-size": 9 });
      }
    }
  }
  // 冲突三角
  S.schedule.conflicts.forEach((c) => {
    const t = S.tasks.find((x) => x.id === c.task_id);
    if (!t) return;
    const y = TL.top + laneOf[t.actor_id] * laneH;
    const color = c.type === "review" ? "#e67e22" : "#e74c3c";
    el("path", { d: `M${xOf(c.time)},${y + 4} l5,-9 l5,9 z`, fill: color }, svg);
  });
}

function startDrag(ev, task) {
  if (task.locked) return; // 已锁节点不得移动
  const w = S.schedule.windows[task.id];
  drag = { id: task.id, dx: ev.clientX - xOf(w.start), moved: false };
  ev.preventDefault();
}

document.addEventListener("pointermove", (ev) => {
  if (!drag) return;
  drag.moved = true;
  const t = Math.max(0, Math.round(tOf(ev.clientX - drag.dx)));
  const w = S.schedule.windows[drag.id];
  w.start = t; w.end = t + (w.end - w.start);
  renderTimeline();
});
document.addEventListener("pointerup", async (ev) => {
  if (!drag) return;
  const d = drag; drag = null;
  if (!d.moved) return;
  const w = S.schedule.windows[d.id];
  const res = await api(`/api/tasks/${d.id}`, "POST", { start_sec: Math.round(w.start) });
  if (res && res.ok === false) { alert(res.error || "更新被拒绝"); return refresh(); }
  await refresh(res);
});

/* ---------------- 侧台平面图 ---------------- */

const EXITS = { L: [2, 10], R: [38, 10] };
const PX = 13.2, PY = 14; // 米→像素

function renderPlan() {
  const svg = $("#plan");
  svg.innerHTML = "";
  el("rect", { x: 0, y: 0, width: 560, height: 320, fill: "#fbfcfe", stroke: "#dde" }, svg);
  el("rect", { x: 12 * PX, y: 6 * PY, width: 16 * PX, height: 8 * PY, fill: "#eef", stroke: "#ccd" }, svg);
  txt(svg, 20 * PX - 14, 10 * PY + 4, "舞台", { fill: "#99a", "font-size": 12 });
  for (const side of ["L", "R"]) {
    const [x, y] = EXITS[side];
    el("circle", { cx: x * PX, cy: y * PY, r: 6, fill: "#34495e" }, svg);
    txt(svg, x * PX - 12, y * PY - 10, side === "L" ? "左口" : "右口", { "font-size": 10, fill: "#34495e" });
  }
  S.carts.forEach((c) => {
    el("rect", { x: c.x * PX - 14, y: c.y * PY - 9, width: 28, height: 18, rx: 3,
                 fill: "#d5f5e3", stroke: "#27ae60" }, svg);
    txt(svg, c.x * PX - 24, c.y * PY + 24, `${c.name}(${c.capacity})`, { "font-size": 9, fill: "#1e8449" });
  });
  S.positions.forEach((p) => {
    const sel = S.tasks.find((t) => t.id === selectedTask && t.position_id === p.id);
    el("rect", { x: p.x * PX - 20, y: p.y * PY - 13, width: 40, height: 26, rx: 4,
                 fill: sel ? "#fdebd0" : "#ebdef0", stroke: sel ? "#e67e22" : "#8e44ad",
                 "stroke-width": sel ? 2.5 : 1 }, svg);
    txt(svg, p.x * PX - 30, p.y * PY + 3, `${p.name}·容${p.capacity}`, { "font-size": 9, fill: "#6c3483" });
  });
  // 选中任务的走位路线：上场口→换装位→(道具车)→上场口
  const t = S.tasks.find((x) => x.id === selectedTask);
  if (t) {
    const p = S.positions.find((x) => x.id === t.position_id);
    const ex = EXITS[t.exit_side] || EXITS.L;
    if (p) {
      const pts = [[ex[0] * PX, ex[1] * PY], [p.x * PX, p.y * PY]];
      const carts = cartsForTask(t);
      carts.forEach((c) => pts.push([c.x * PX, c.y * PY]));
      pts.push([ex[0] * PX, ex[1] * PY]);
      el("polyline", { points: pts.map((q) => q.join(",")).join(" "), fill: "none",
                       stroke: "#e67e22", "stroke-width": 2, "stroke-dasharray": "5 3" }, svg);
    }
  }
}

function cartsForTask(task) {
  const look = S.looks.find((l) => l.actor_id === task.actor_id && l.scene_id === task.to_scene_id);
  if (!look) return [];
  const prev = S.looks.find((l) => l.actor_id === task.actor_id && l.scene_id === task.from_scene_id);
  const prevIds = new Set(prev ? S.look_items.filter((li) => li.look_id === prev.id).map((li) => li.item_id) : []);
  const ids = S.look_items.filter((li) => li.look_id === look.id && !prevIds.has(li.item_id)).map((li) => li.item_id);
  const carts = new Map();
  ids.forEach((iid) => {
    const it = S.items.find((i) => i.id === iid);
    if (it && it.kind === "prop" && it.cart_id) {
      const c = S.carts.find((x) => x.id === it.cart_id);
      if (c) carts.set(c.id, c);
    }
  });
  return [...carts.values()];
}

/* ---------------- 服装层级卡 ---------------- */

function renderLayers() {
  const box = $("#layer-cards");
  box.innerHTML = "";
  const t = S.tasks.find((x) => x.id === selectedTask);
  if (!t) { box.innerHTML = '<p class="muted">未选中任务</p>'; return; }
  const itemsOf = (sceneId) => {
    const look = S.looks.find((l) => l.actor_id === t.actor_id && l.scene_id === sceneId);
    if (!look) return [];
    return S.look_items.filter((li) => li.look_id === look.id)
      .map((li) => S.items.find((i) => i.id === li.item_id))
      .filter(Boolean)
      .sort((a, b) => a.layer - b.layer);
  };
  const prev = itemsOf(t.from_scene_id), next = itemsOf(t.to_scene_id);
  const nextIds = new Set(next.map((i) => i.id)), prevIds = new Set(prev.map((i) => i.id));
  const doff = prev.filter((i) => !nextIds.has(i.id)).sort((a, b) => b.layer - a.layer);
  const don = next.filter((i) => !prevIds.has(i.id));
  box.appendChild(layerCol("脱下（外→内）", doff, "doff_sec"));
  box.appendChild(layerCol("穿上（内→外）", don, "don_sec"));
}

function layerCol(title, items, key) {
  const col = document.createElement("div");
  col.className = "layer-col";
  col.innerHTML = `<h4>${title}</h4>`;
  if (!items.length) col.innerHTML += '<p class="muted">无</p>';
  items.forEach((i) => {
    const d = document.createElement("div");
    d.className = `lcard ${i.kind} ${i.status !== "ok" ? i.status : ""}`;
    const st = i.status === "ok" ? "" : i.status === "cleaning" ? "·清洁中" : "·维修中";
    d.innerHTML = `<b>${i.name}</b> <span class="t">L${i.layer} ${i[key]}s${st}</span>`;
    col.appendChild(d);
  });
  return col;
}

/* ---------------- 任务详情 ---------------- */

function renderDetail() {
  const box = $("#task-detail");
  const t = S.tasks.find((x) => x.id === selectedTask);
  if (!t) { box.innerHTML = '<p class="muted">在时间轴上选择一个任务</p>'; return; }
  const w = S.schedule.windows[t.id];
  const actor = S.actors.find((a) => a.id === t.actor_id);
  const fs = S.scenes.find((s) => s.id === t.from_scene_id);
  const ts = S.scenes.find((s) => s.id === t.to_scene_id);
  const opts = (list, cur, none) =>
    `<option value="">${none}</option>` +
    list.map((o) => `<option value="${o.id}" ${o.id === cur ? "selected" : ""}>${o.name}</option>`).join("");
  box.innerHTML = `
    <div><b>#${t.id} ${actor.name}</b>：${fs.name} → ${ts.name}
      ${t.locked ? '<span class="badge lock">已锁</span>' : ""}
      ${t.needs_review ? '<span class="badge review">待复核</span>' : ""}</div>
    <div class="muted">窗口 ${fmt(w.ready)} → ${fmt(w.deadline)}｜计划 ${fmt(w.start)} → ${fmt(w.end)}
      ${w.ok ? "" : "｜<b style='color:#c0392b'>无法按时</b>"}</div>
    <label>服装师</label><select id="d-dresser">${opts(S.dressers, t.dresser_id, "自助")}</select>
    <label>换装位</label><select id="d-position">${opts(S.positions, t.position_id, "未指定")}</select>
    <label>出入口</label><select id="d-side">
      <option value="L" ${t.exit_side === "L" ? "selected" : ""}>左口</option>
      <option value="R" ${t.exit_side === "R" ? "selected" : ""}>右口</option></select>
    <label>开始时间(秒)</label><input id="d-start" type="number" value="${Math.round(w.start)}">
    <div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">
      <button id="d-apply">应用</button>
      <button id="d-lock" class="warn">${t.locked ? "解锁" : "锁定（连排确认）"}</button>
      ${t.needs_review ? '<button id="d-review">复核完成</button>' : ""}
    </div>
    <div class="acts">${S.schedule.actions.filter((a) => a.task_id === t.id)
      .map((a) => `${fmt(a.start)} ${a.label}(${a.dur}s)`).join("<br>")}</div>`;
  $("#d-apply").onclick = async () => {
    const v = (id) => { const x = $(id).value; return x === "" ? null : (isNaN(+x) ? x : +x); };
    const res = await api(`/api/tasks/${t.id}`, "POST", {
      dresser_id: v("#d-dresser"), position_id: v("#d-position"),
      exit_side: $("#d-side").value, start_sec: v("#d-start"),
    });
    if (res && res.ok === false) { alert(res.error || "更新被拒绝"); return refresh(); }
    await refresh(res);
  };
  $("#d-lock").onclick = async () =>
    refresh(await api(`/api/tasks/${t.id}`, "POST", { locked: t.locked ? 0 : 1 }));
  const rv = $("#d-review");
  if (rv) rv.onclick = async () => refresh(await api(`/api/tasks/${t.id}/clear_review`, "POST"));
}

/* ---------------- 冲突与建议 ---------------- */

function renderConflicts() {
  const box = $("#conflicts");
  const cs = S.schedule.conflicts;
  box.innerHTML = cs.length ? "" : '<p class="muted">当前无冲突 ✓</p>';
  cs.forEach((c) => {
    const d = document.createElement("div");
    d.className = "conflict" + (c.type === "review" ? " review" : "");
    d.textContent = `[${fmt(c.time)}] 任务#${c.task_id} ${c.message}`;
    d.onclick = () => { selectedTask = c.task_id; renderAll(); };
    box.appendChild(d);
  });
  const sg = $("#suggestions");
  sg.innerHTML = "";
  (S.suggestions || []).forEach((s) => {
    const d = document.createElement("div");
    d.className = "suggestion";
    const pos = S.positions.find((p) => p.id === s.changes.position_id);
    const dr = S.dressers.find((x) => x.id === s.changes.dresser_id);
    d.innerHTML = `替代排法：任务#${s.task_id} → 换装位「${pos ? pos.name : "-"}」` +
      `｜服装师「${dr ? dr.name : "自助"}」｜开始 ${fmt(s.changes.start_sec)}` +
      `（剩余冲突 ${s.remaining_conflicts}）`;
    const b = document.createElement("button");
    b.textContent = "采用";
    b.onclick = async () => refresh(await api("/api/apply_suggestion", "POST", s));
    d.appendChild(b);
    sg.appendChild(d);
  });
}

/* ---------------- 录入表单 ---------------- */

const FORMS = [
  ["scenes", "场次", [["seq", "序号", "number"], ["name", "名称"], ["start_sec", "开始(秒)", "number"], ["duration_sec", "时长(秒)", "number"]]],
  ["actors", "演员", [["name", "姓名"], ["code", "代号"], ["default_side", "默认出入口(L/R)"]]],
  ["dressers", "服装师", [["name", "姓名"]]],
  ["positions", "换装位", [["name", "名称"], ["side", "侧(L/R)"], ["x", "X(米)", "number"], ["y", "Y(米)", "number"], ["capacity", "容量", "number"]]],
  ["carts", "服装车", [["name", "名称"], ["side", "侧(L/R)"], ["x", "X(米)", "number"], ["y", "Y(米)", "number"], ["capacity", "容量", "number"]]],
  ["items", "服装/道具", [["name", "名称"], ["kind", "类型(costume/shoes/prop)"], ["layer", "层级", "number"],
    ["don_sec", "穿上秒", "number"], ["doff_sec", "脱下秒", "number"], ["status", "状态(ok/cleaning/repair)"],
    ["available_at", "可用时刻(秒)", "number"], ["cart_id", "服装车ID", "number"], ["copies", "件数", "number"]]],
  ["looks", "角色造型", [["actor_id", "演员ID", "number"], ["scene_id", "场次ID", "number"], ["name", "造型名"]]],
  ["tasks", "换装任务", [["actor_id", "演员ID", "number"], ["from_scene_id", "从场次ID", "number"],
    ["to_scene_id", "至场次ID", "number"], ["exit_side", "出入口(L/R)"], ["position_id", "换装位ID", "number"],
    ["dresser_id", "服装师ID", "number"]]],
];

function renderEntry() {
  const box = $("#entry-forms");
  box.innerHTML = "";
  FORMS.forEach(([entity, title, fields]) => {
    const f = document.createElement("div");
    f.className = "eform";
    f.innerHTML = `<h4>${title}</h4>` + fields.map(([k, ph, tp]) =>
      `<input data-k="${k}" type="${tp || "text"}" placeholder="${ph}">`).join("") +
      `<button>添加</button>`;
    f.querySelector("button").onclick = async () => {
      const data = {};
      f.querySelectorAll("input").forEach((i) => {
        if (i.value !== "") data[i.dataset.k] = i.type === "number" ? +i.value : i.value;
      });
      await api(`/api/${entity}`, "POST", data);
      await refresh();
    };
    box.appendChild(f);
  });
  // 造型-服装关联
  const lf = document.createElement("div");
  lf.className = "eform";
  lf.innerHTML = `<h4>造型↔服装</h4>
    <select id="li-look">${S.looks.map((l) => {
      const a = S.actors.find((x) => x.id === l.actor_id);
      const sc = S.scenes.find((x) => x.id === l.scene_id);
      return `<option value="${l.id}">#${l.id} ${a ? a.name : "?"}·${sc ? sc.name : "?"}·${l.name}</option>`;
    }).join("")}</select>
    <select id="li-items" multiple size="6">${S.items.map((i) =>
      `<option value="${i.id}">${i.name}(L${i.layer})</option>`).join("")}</select>
    <button>保存清单</button>`;
  lf.querySelector("button").onclick = async () => {
    const ids = [...lf.querySelector("#li-items").selectedOptions].map((o) => +o.value);
    await refresh(await api("/api/look_items", "POST", { look_id: +lf.querySelector("#li-look").value, item_ids: ids }));
  };
  box.appendChild(lf);
  // 服装状态修改
  const sf = document.createElement("div");
  sf.className = "eform";
  sf.innerHTML = `<h4>服装状态/服装车调整</h4>
    <select id="it-sel">${S.items.map((i) => `<option value="${i.id}">#${i.id} ${i.name}（${i.status}）</option>`).join("")}</select>
    <select id="it-status"><option value="ok">ok</option><option value="cleaning">cleaning</option><option value="repair">repair</option></select>
    <input id="it-avail" type="number" placeholder="可用时刻(秒)">
    <select id="it-cart"><option value="">服装车…</option>${S.carts.map((c) => `<option value="${c.id}">${c.name}</option>`).join("")}</select>
    <button>更新</button>`;
  sf.querySelector("button").onclick = async () => {
    const data = { status: sf.querySelector("#it-status").value };
    const av = sf.querySelector("#it-avail").value, ct = sf.querySelector("#it-cart").value;
    if (av !== "") data.available_at = +av;
    if (ct !== "") data.cart_id = +ct;
    await refresh(await api(`/api/items/${sf.querySelector("#it-sel").value}`, "POST", data));
  };
  box.appendChild(sf);
  // 场次时间调整（触发关联任务待复核）
  const cf = document.createElement("div");
  cf.className = "eform";
  cf.innerHTML = `<h4>场次时间调整</h4>
    <select id="sc-sel">${S.scenes.map((s) => `<option value="${s.id}">#${s.id} ${s.name}</option>`).join("")}</select>
    <input id="sc-start" type="number" placeholder="开始(秒)">
    <input id="sc-dur" type="number" placeholder="时长(秒)">
    <button>更新（关联任务待复核）</button>`;
  cf.querySelector("button").onclick = async () => {
    const data = {};
    if (cf.querySelector("#sc-start").value !== "") data.start_sec = +cf.querySelector("#sc-start").value;
    if (cf.querySelector("#sc-dur").value !== "") data.duration_sec = +cf.querySelector("#sc-dur").value;
    await refresh(await api(`/api/scenes/${cf.querySelector("#sc-sel").value}`, "POST", data));
  };
  box.appendChild(cf);
}

function renderRevisions() {
  const ul = $("#revisions");
  ul.innerHTML = "";
  S.revisions.forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `#${r.id} ${new Date(r.created_at * 1000).toLocaleString()} ${r.note}`;
    const b = document.createElement("button");
    b.textContent = "恢复";
    b.onclick = async () => { if (confirm("恢复该修订？当前场次/任务/服装将被覆盖")) refresh(await api(`/api/revisions/${r.id}/restore`, "POST")); };
    li.appendChild(b);
    ul.appendChild(li);
  });
}

/* ---------------- 连排实测 ---------------- */

function renderRunBar() {
  const rev = $("#run-rev");
  rev.innerHTML = S.revisions.length
    ? S.revisions.map((r) => `<option value="${r.id}">修订#${r.id} ${r.note || ""}</option>`).join("")
    : '<option value="">（先保存修订作为基准）</option>';
  const sel = $("#run-sel");
  sel.innerHTML = '<option value="">不查看连排</option>' +
    S.runs.map((r) => `<option value="${r.id}" ${r.id === selectedRun ? "selected" : ""}>` +
      `#${r.id} ${r.name}${r.status === "open" ? "（进行中）" : "（已结束）"}</option>`).join("");
  $("#btn-run-close").disabled = !(R && R.run.status === "open");
}

async function loadRun(id) {
  selectedRun = id;
  R = id ? await api(`/api/runs/${id}`) : null;
  renderAll();
}

function parseClock() {
  const v = $("#run-clock").value.trim();
  let m;
  if (/^\d+$/.test(v)) return +v;
  if ((m = v.match(/^(\d+):(\d{1,2})$/))) return +m[1] * 60 + +m[2];
  alert("时钟格式：mm:ss 或秒数");
  return null;
}

async function punch(tid, idx, kind, existing) {
  if (!R || R.run.status !== "open") return alert("连排未在进行中");
  const at = parseClock();
  if (at === null) return;
  let reason = "";
  if (kind === "exception") {
    reason = prompt("异常理由（必填）：", "");
    if (!reason) return alert("异常打点必须填写理由");
  } else if (existing) {
    reason = prompt(`补正理由（必填，原记录 ${fmt(existing.at_sec)} 保留备查）：`, "");
    if (!reason) return alert("补正必须填写理由");
  }
  const res = await api(`/api/runs/${R.run.id}/events`, "POST",
    { task_id: tid, action_idx: idx, kind, at_sec: at, reason });
  if (res && res.ok === false) return alert(res.error || "打点被拒绝");
  R = res.run;
  renderAll();
}

function renderPunch() {
  const box = $("#run-punch");
  if (!R) { box.innerHTML = '<p class="muted">未选择连排</p>'; return; }
  const open = R.run.status === "open";
  const tasks = R.plan.tasks.slice().sort((a, b) =>
    (R.plan.windows[a.id] || {}).start - (R.plan.windows[b.id] || {}).start);
  const cur = tasks.find((t) => t.id === selectedTask) || tasks[0];
  if (!cur) { box.innerHTML = '<p class="muted">基准计划中没有任务</p>'; return; }
  const actor = (id) => (R.plan.actors.find((a) => a.id === id) || {}).name || "?";
  let html = `<div class="muted">基准修订#${R.run.revision_id}｜${R.run.name}｜` +
    `${open ? "进行中" : "已结束（只读）"}</div>`;
  html += `<select id="punch-task">` + tasks.map((t) => {
    const w = R.plan.windows[t.id] || {};
    return `<option value="${t.id}" ${t.id === cur.id ? "selected" : ""}>` +
      `#${t.id} ${actor(t.actor_id)} 计划${fmt(w.start || 0)}</option>`;
  }).join("") + `</select>`;
  const acts = R.plan.actions.filter((a) => a.task_id === cur.id)
    .sort((a, b) => a.idx - b.idx);
  const superseded = new Set(R.events.filter((e) => e.supersedes).map((e) => e.supersedes));
  html += acts.map((a) => {
    const ac = R.analysis.actuals[`${cur.id}:${a.idx}`] || {};
    const eff = (k) => R.events.find((e) => e.task_id === cur.id && e.action_idx === a.idx &&
      e.kind === k && !superseded.has(e.id));
    const evS = eff("start"), evD = eff("done"), evK = eff("skip");
    const exc = R.events.filter((e) => e.task_id === cur.id && e.action_idx === a.idx &&
      e.kind === "exception" && !superseded.has(e.id));
    const state = ac.skipped ? '<span class="tag skip">已跳过</span>'
      : `${evS ? fmt(evS.at_sec) : "—"} → ${evD ? fmt(evD.at_sec) : "—"}`;
    const btns = open ? ["start", "done", "skip", "exception"].map((k) => {
      const label = { start: "开始", done: "完成", skip: "跳过", exception: "异常" }[k];
      const ex = eff(k);
      return `<button class="pbtn ${k}" data-t="${cur.id}" data-i="${a.idx}" data-k="${k}" ` +
        `data-ex="${ex ? ex.at_sec : ""}">${ex ? "补正" : label}</button>`;
    }).join("") : "";
    const notes = exc.map((e) => `<span class="tag exc">⚑${e.reason}</span>`).join("") +
      [evS, evD, evK].filter((e) => e && e.reason)
        .map((e) => `<span class="tag corr">补正:${e.reason}</span>`).join("");
    return `<div class="punch-row"><span class="pidx">${a.idx}</span>` +
      `<span class="plabel">${a.label}</span><span class="pplan">计划 ${fmt(a.start)} (${a.dur}s)</span>` +
      `<span class="pstate">${state}</span>${notes}<span class="pbtns">${btns}</span></div>`;
  }).join("");
  box.innerHTML = html;
  const sel = $("#punch-task");
  sel.onchange = () => { selectedTask = +sel.value; renderAll(); };
  box.querySelectorAll(".pbtn").forEach((b) => {
    b.onclick = () => punch(+b.dataset.t, +b.dataset.i, b.dataset.k,
      b.dataset.ex !== "" ? { at_sec: +b.dataset.ex } : null);
  });
}

function renderRunChecks() {
  const box = $("#run-checks");
  if (!R) { box.innerHTML = '<p class="muted">未选择连排</p>'; return; }
  const ana = R.analysis;
  let html = "";
  const fd = ana.first_deviation;
  html += fd
    ? `<div class="deviation">首个偏差：任务#${fd.task_id} 动作${fd.action_idx}「${fd.label}」` +
      ` @ ${fmt(fd.time)} — ${fd.message}</div>`
    : '<div class="muted">暂无显著偏差（阈值 ±5s）</div>';
  if (ana.chain.length) {
    html += '<div class="chain"><b>后续等待链：</b><ol>' +
      ana.chain.map((c) => `<li>[${fmt(c.time)}] ${c.message}</li>`).join("") + "</ol></div>";
  }
  const TYPE = { order: "时刻倒序", missing: "动作漏项", concurrency: "并发冲突", item: "错用服装" };
  html += ana.anomalies.length
    ? ana.anomalies.map((c) =>
      `<div class="anomaly ${c.type}" data-t="${c.task_id}">` +
      `<b>${TYPE[c.type] || c.type}</b> [${fmt(c.time)}] 任务#${c.task_id} ${c.message}</div>`).join("")
    : '<div class="muted">实测检查通过 ✓</div>';
  box.innerHTML = html;
  box.querySelectorAll(".anomaly").forEach((d) => {
    d.onclick = () => { selectedTask = +d.dataset.t; renderAll(); };
  });
}

async function renderSummary() {
  const box = $("#run-summary");
  const res = await api("/api/runs/summary");
  const sg = res.suggestions || [];
  if (!sg.length) {
    box.innerHTML = '<p class="muted">暂无建议（需要已结束的连排，且实测 P75 与基准不同）</p>';
    return;
  }
  box.innerHTML = `<table class="sumtab"><tr><th></th><th>服装</th><th>动作</th><th>配置</th>` +
    `<th>基准</th><th>建议</th><th>样本</th></tr>` + sg.map((s) =>
      `<tr><td><input type="checkbox" class="sumchk" data-k="${s.key}"></td>` +
      `<td>${s.item_name}</td><td>${s.action === "don" ? "穿上" : "脱下"}</td>` +
      `<td>${s.staffed ? "有服装师" : "自助"}</td><td>${s.current}s</td>` +
      `<td><b>${s.suggested}s</b></td><td>${s.n} 次（${s.min}–${s.max}s）</td></tr>`).join("") +
    `</table><button id="btn-derive">勾选建议并派生修订（只重排受影响任务，已锁不动）</button>`;
  $("#btn-derive").onclick = async () => {
    const keys = [...box.querySelectorAll(".sumchk")].filter((c) => c.checked)
      .map((c) => c.dataset.k);
    if (!keys.length) return alert("请先勾选建议");
    const res2 = await api("/api/runs/derive", "POST", { keys });
    if (res2 && res2.ok === false) return alert(res2.error || "派生失败");
    alert(res2.derived ? res2.derived.note : "已派生");
    await refresh(res2);
  };
}



function renderAll() {
  renderTimeline();
  renderPlan();
  renderLayers();
  renderDetail();
  renderConflicts();
  renderEntry();
  renderRevisions();
  renderRunBar();
  renderPunch();
  renderRunChecks();
  renderSummary();
  const sel = $("#export-actor");
  sel.innerHTML = S.actors.map((a) => `<option value="${a.id}">${a.name}</option>`).join("");
}

$("#btn-reschedule").onclick = async () => refresh(await api("/api/reschedule", "POST"));
$("#btn-save-rev").onclick = async () => {
  const note = prompt("修订说明：", "连排前存档");
  if (note !== null) refresh(await api("/api/revisions", "POST", { note }));
};
$("#btn-cue").onclick = () => window.open(`/export/cue/${$("#export-actor").value}`);
$("#btn-flow").onclick = () => (location.href = "/export/flow.csv");
$("#btn-svg").onclick = () => (location.href = "/export/timeline.svg?dl=1");

$("#btn-run-start").onclick = async () => {
  const rid = $("#run-rev").value;
  if (!rid) return alert("请先保存一个修订作为连排基准");
  const res = await api("/api/runs", "POST",
    { revision_id: +rid, name: $("#run-name").value.trim() });
  if (res && res.ok === false) return alert(res.error || "开启失败");
  R = res.run;
  selectedRun = R.run.id;
  $("#run-name").value = "";
  await refresh();
};
$("#run-sel").onchange = (ev) => loadRun(ev.target.value ? +ev.target.value : null);
$("#btn-run-close").onclick = async () => {
  if (!R || !confirm("结束本次连排？结束后不能再打点")) return;
  const res = await api(`/api/runs/${R.run.id}/close`, "POST");
  if (res && res.ok === false) return alert(res.error || "操作失败");
  R = res.run;
  await refresh();
};
$("#btn-run-svg").onclick = () =>
  R ? window.open(`/export/run/${R.run.id}/compare.svg?dl=1`) : alert("请先选择连排");
$("#btn-run-record").onclick = () =>
  R ? window.open(`/export/run/${R.run.id}/record`) : alert("请先选择连排");
$("#btn-clock-now").onclick = () => {
  const t = S.tasks.find((x) => x.id === selectedTask);
  const w = t && S.schedule.windows[t.id];
  if (w) $("#run-clock").value = fmt(Math.round(w.start));
};

refresh();
