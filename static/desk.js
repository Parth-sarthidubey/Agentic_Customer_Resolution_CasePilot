/* CasePilot desk - vanilla JS SPA (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const view = $("#view");
let timers = [];

const icon = (id, w = 14) => `<svg viewBox="0 0 24 24" width="${w}" height="${w}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><use href="#g-${id}"/></svg>`;

const AGENTS = {
  "Intake Agent": { i: "IN", c: "#6366F1", role: "Understands the goal, binds records" },
  "Investigator Agent": { i: "IV", c: "#4338CA", role: "Gathers facts & eligible options" },
  "Resolver Agent": { i: "RS", c: "#7C3AED", role: "Plans state-changing actions" },
  "Policy Auditor": { i: "PA", c: "#059669", role: "Independent policy review" },
  "Executor": { i: "EX", c: "#D97706", role: "Runs actions, retries safely" },
  "Verifier": { i: "VF", c: "#0D9488", role: "Checks systems of record" },
  "Communicator Agent": { i: "CM", c: "#0EA5E9", role: "Replies & records learnings" },
};
const OTHERS = {
  "Orchestrator": { i: "OR", c: "#475569" }, "CasePilot": { i: "CP", c: "#4338CA" }, "Environment": { i: "EV", c: "#D97706" },
  "Supervisor": { i: "SV", c: "#E11D48" }, "Customer": { i: "CU", c: "#64748B" }, "Unassigned": { i: "—", c: "#CBD5E1" },
  "Trust & Safety": { i: "TS", c: "#E11D48" }, "Support Lead": { i: "SL", c: "#E11D48" },
};
const initials = (n) => (n || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
const who = (n) => AGENTS[n] || OTHERS[n] || { i: initials(n), c: "#64748B" };
const avatar = (n, extra = "") => { const a = who(n); return `<span class="avatar ${extra}" style="background:${a.c}" title="${esc(n)}">${a.i}</span>`; };

const COLUMNS = [
  { title: "Triage", st: ["New", "Triage"], c: "#64748B" },
  { title: "Investigating", st: ["Investigating", "Planning", "Policy Review"], c: "#4338CA" },
  { title: "Needs human", st: ["Awaiting Approval", "Waiting on Customer"], c: "#D97706" },
  { title: "Executing", st: ["Executing", "Verifying"], c: "#6366F1" },
  { title: "Resolved", st: ["Resolved"], c: "#059669" },
  { title: "With a human", st: ["Escalated", "Routed"], c: "#E11D48" },
];
const ACTIVE = ["Triage", "Investigating", "Planning", "Policy Review", "Executing", "Verifying"];
function lz(status) {
  const cls = status === "Resolved" ? "lz-done" : status === "Escalated" ? "lz-danger"
    : ["Awaiting Approval", "Waiting on Customer", "Routed"].includes(status) ? "lz-human"
    : status === "New" ? "lz-new" : "lz-progress";
  return `<span class="lozenge ${cls}">${esc(status)}</span>`;
}
const prio = (p) => p ? `<span class="prio ${p}"><i></i>${p}</span>` : `<span class="muted small">—</span>`;

// ---------- helpers ----------
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json) { init.body = JSON.stringify(opts.json); init.headers = { "Content-Type": "application/json" }; }
  const r = await fetch(path, init);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) { /* */ } throw new Error(m); }
  return r.json();
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(t) {
  let html = "", list = false;
  for (const raw of esc(t).split("\n")) {
    const l = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
    const li = l.match(/^\s*[-•]\s+(.*)/);
    if (li) { if (!list) { html += "<ul>"; list = true; } html += `<li>${li[1]}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    if (l.trim()) html += `<p>${l}</p>`;
  }
  return `<div class="md">${html}${list ? "</ul>" : ""}</div>`;
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now"; if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return new Date(iso).toLocaleDateString();
}
const money = (n) => (n == null ? "—" : Number(n).toFixed(2));
function toast(m) { const t = $("#toast"); t.textContent = m; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 3200); }
function every(ms, fn) { fn(); timers.push(setInterval(fn, ms)); }
function modal(html) { $("#modal-card").innerHTML = html; $("#modal").hidden = false; }
function closeModal() { $("#modal").hidden = true; }
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
$("#menu-btn").onclick = () => $("#sidebar").classList.toggle("open");

// ---------- router ----------
const routes = { board: renderBoard, queue: renderQueue, case: renderCase, systems: renderSystems, knowledge: renderKnowledge };
function route() {
  timers.forEach(clearInterval); timers = [];
  const [name, arg] = (location.hash.slice(2) || "board").split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === name));
  $("#sidebar").classList.remove("open");
  (routes[name] || renderBoard)(arg);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

// ---------- global chrome: status, team, demo menu ----------
let CASES = [];
async function refreshGlobal() {
  try { CASES = await api("/api/cases"); } catch (e) { return; }
  $("#queue-count").textContent = CASES.filter((c) => c.status !== "Resolved").length || "";
  $("#team").innerHTML = Object.entries(AGENTS).map(([n, a]) => {
    const busy = CASES.filter((c) => c.assignee === n && c.running);
    return `<div class="member ${busy.length ? "busy" : ""}">${avatar(n, busy.length ? "working" : "")}
      <div><div class="m-name">${esc(n.replace(" Agent", ""))}</div><div class="m-state">${busy.length ? "Working on " + busy.map((c) => c.number).join(", ") : esc(a.role)}</div></div></div>`;
  }).join("");
}
(async function chrome() {
  const s = await api("/api/status");
  const p = s.llm.providers;
  $("#model-pill").innerHTML = s.llm.mode === "offline" ? `<span class="dot off"></span><b>Offline rules</b> · no LLM key`
    : `<span class="dot"></span><b>${esc(p[0].name)}</b> ${esc(p[0].model)}${p.length > 1 ? ` → ${p.slice(1).map((x) => esc(x.name)).join(" → ")}` : ""} → rules`;
  $("#lim-refund").textContent = s.limits.auto_refund; $("#lim-credit").textContent = s.limits.auto_credit;
  const sc = await api("/api/scenarios");
  $("#demo-menu").innerHTML = `<div class="dm-head">File a sample customer case</div>` + sc.map((x) =>
    `<button data-sc="${x.key}"><b>${esc(x.label)}</b><span>${esc(x.customer)} · "${esc(x.subject)}"</span><span>${esc(x.shows)}</span></button>`).join("") +
    `<div class="dm-head" style="margin-top:6px">Environment</div><button data-reset="1"><b>Reset demo environment</b><span>Wipe cases and restore the enterprise sandbox</span></button>`;
  $("#demo-menu").onclick = async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $("#demo-menu").hidden = true;
    if (b.dataset.reset) { if (confirm("Reset all cases and enterprise data?")) { await api("/api/reset", { method: "POST" }); toast("Environment reset"); route(); } return; }
    const c = await api(`/api/scenarios/${b.dataset.sc}`, { method: "POST" });
    toast(`${c.number} filed by ${c.customer_name} - agents picking it up`);
    location.hash = `#/case/${c.id}`;
  };
})();
$("#demo-btn").onclick = (e) => { e.stopPropagation(); $("#demo-menu").hidden = !$("#demo-menu").hidden; };
document.addEventListener("click", (e) => { if (!e.target.closest(".dropdown")) $("#demo-menu").hidden = true; });
setInterval(refreshGlobal, 2000); refreshGlobal();

// ---------- create case ----------
$("#create-btn").onclick = async () => {
  const customers = await api("/api/customers");
  modal(`<header><h2>Create case</h2></header>
    <form id="cf"><div class="m-body">
      <div class="field"><label>Customer <em>*</em></label><select name="customer">${customers.map((c) => `<option value="${esc(c.email)}|${esc(c.name)}">${esc(c.name)} · ${esc(c.email)} · ${esc(c.tier)}</option>`).join("")}<option value="other">Other email…</option></select></div>
      <div class="field" id="other-email" hidden><label>Customer email</label><input name="email" type="email" placeholder="name@example.com"></div>
      <div class="field"><label>Channel</label><select name="channel"><option value="email">Email</option><option value="portal">Web portal</option><option value="chat">Chat</option><option value="phone">Phone</option></select></div>
      <div class="field"><label>Summary <em>*</em></label><input name="subject" required placeholder="e.g. Kettle arrived cracked"></div>
      <div class="field"><label>Customer message <em>*</em></label><textarea name="description" required placeholder="What the customer wrote…"></textarea></div>
      <div class="field"><label>Attachments</label><div class="drop" id="drop">Drop photos, receipts or documents here, or <a href="#" id="pick">browse</a><input type="file" id="files" multiple hidden></div><div class="file-list" id="file-list"></div></div>
    </div><footer><button type="button" class="btn subtle" onclick="closeModal()">Cancel</button><button class="btn primary">Create</button></footer></form>`);
  const f = $("#cf"); let picked = [];
  f.customer.onchange = () => ($("#other-email").hidden = f.customer.value !== "other");
  const show = () => ($("#file-list").textContent = picked.map((x) => `${x.name} (${Math.round(x.size / 1024)} KB)`).join("   "));
  $("#pick").onclick = (e) => { e.preventDefault(); $("#files").click(); };
  $("#files").onchange = (e) => { picked = [...picked, ...e.target.files]; show(); };
  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); picked = [...picked, ...e.dataTransfer.files]; show(); };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData();
    let [email, name] = f.customer.value.split("|");
    if (f.customer.value === "other") { email = f.email.value; name = ""; }
    fd.append("customer_email", email); fd.append("customer_name", name || "");
    fd.append("subject", f.subject.value); fd.append("description", f.description.value); fd.append("channel", f.channel.value);
    picked.forEach((x) => fd.append("files", x));
    const c = await api("/api/cases", { method: "POST", body: fd });
    closeModal(); toast(`${c.number} created - agents are on it`); location.hash = `#/case/${c.id}`;
  };
};

// ---------- board (Jira-style) ----------
let lastCol = {};
function renderBoard() {
  view.innerHTML = `<div class="breadcrumb">Projects / Kestrel Home Support</div>
    <div class="page-head"><h1>Resolution board</h1><div class="spacer"></div><input class="search" id="q" placeholder="Search cases"></div>
    <div class="board" id="board"></div>`;
  const draw = () => {
    const q = ($("#q")?.value || "").toLowerCase();
    const list = CASES.filter((c) => !q || `${c.number} ${c.subject} ${c.customer_name}`.toLowerCase().includes(q));
    $("#board").innerHTML = COLUMNS.map((col, ci) => {
      const cards = list.filter((c) => col.st.includes(c.status));
      return `<div class="column" style="--col:${col.c}"><div class="column-head">${col.title} <span class="count">${cards.length}</span></div>
        ${cards.map((c) => {
          const moved = lastCol[c.id] !== undefined && lastCol[c.id] !== ci; lastCol[c.id] = ci;
          return `<div class="card ${moved ? "moved" : ""}" onclick="location.hash='#/case/${c.id}'">
            <div class="c-title">${esc(c.subject)}</div>
            <div class="c-meta"><span class="label chan chan-${esc(c.channel)}">${esc(c.channel)}</span>${c.goal ? `<span class="label">${esc(c.goal.replace(/_/g, " "))}</span>` : ""}${c.order_id ? `<span class="label">${esc(c.order_id)}</span>` : ""}${c.attachments ? `<span class="label">${icon("clip", 11)} ${c.attachments}</span>` : ""}</div>
            ${c.running ? `<div class="c-status"><span class="typing"><i></i><i></i><i></i></span>${esc(c.assignee)} · ${esc(c.status)}</div>` : ["Awaiting Approval", "Waiting on Customer", "Escalated", "Routed"].includes(c.status) ? `<div style="margin-top:6px">${lz(c.status)}</div>` : ""}
            <div class="c-who">${esc(c.customer_name || "")}</div>
            <div class="c-foot"><span class="c-key">${esc(c.number)}</span>${prio(c.priority)}<span class="spacer"></span>${avatar(c.assignee, c.running ? "working" : "")}</div></div>`;
        }).join("") || `<div class="empty-col">No cases</div>`}</div>`;
    }).join("");
  };
  $("#q").oninput = draw;
  every(1500, async () => { await refreshGlobal(); draw(); });
}

// ---------- queue (classic ITSM list) ----------
let queueFilter = "all";
function renderQueue() {
  const F = { all: ["All", () => true], active: ["Active", (c) => !["Resolved", "Escalated"].includes(c.status)],
    human: ["Needs human", (c) => ["Awaiting Approval", "Waiting on Customer", "Escalated", "Routed"].includes(c.status)],
    resolved: ["Resolved", (c) => c.status === "Resolved"] };
  view.innerHTML = `<div class="breadcrumb">Projects / Kestrel Home Support</div><div class="page-head"><h1>Case queue</h1><div class="spacer"></div>
    ${Object.entries(F).map(([k, [l]]) => `<button class="chip-toggle ${k === queueFilter ? "on" : ""}" data-f="${k}">${l}</button>`).join("")}<input class="search" id="q" placeholder="Search"></div>
    <div class="list-wrap"><div class="list-bar"><b>Cases</b><span class="crumbs">All &gt; <a href="#/queue">${F[queueFilter][0]}</a></span><span class="spacer"></span><span id="n" class="muted"></span></div><div id="tbl"></div></div>`;
  view.querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => { queueFilter = b.dataset.f; renderQueue(); }));
  const draw = () => {
    const q = ($("#q")?.value || "").toLowerCase();
    const rows = CASES.filter(F[queueFilter][1]).filter((c) => !q || JSON.stringify(c).toLowerCase().includes(q));
    $("#n").textContent = `${rows.length} records`;
    $("#tbl").innerHTML = `<table class="list"><thead><tr><th>Number</th><th>Short description</th><th>Customer</th><th>Channel</th><th>Priority</th><th>State</th><th>Assigned to</th><th>Goal</th><th>Order</th><th>Updated</th></tr></thead><tbody>
      ${rows.map((c) => `<tr class="click" onclick="location.hash='#/case/${c.id}'"><td><a class="num" href="#/case/${c.id}">${esc(c.number)}</a></td><td class="wrap">${esc(c.subject)}</td>
      <td>${esc(c.customer_name)}</td><td>${esc(c.channel)}</td><td>${prio(c.priority)}</td><td>${lz(c.status)}</td>
      <td><span style="display:inline-flex;gap:6px;align-items:center">${avatar(c.assignee, c.running ? "working" : "")}${esc(c.assignee)}</span></td>
      <td>${esc((c.goal || "").replace(/_/g, " "))}</td><td class="mono">${esc(c.order_id || "")}</td><td class="muted">${ago(c.updated_at)}</td></tr>`).join("") ||
      `<tr><td colspan="10" class="muted" style="padding:24px;text-align:center">No cases yet. Use Demo cases or Create.</td></tr>`}</tbody></table>`;
  };
  $("#q").oninput = draw;
  every(2000, async () => { await refreshGlobal(); draw(); });
}

// ---------- case view ----------
const FLOW = [["Triage", "Intake"], ["Investigating", "Investigation"], ["Planning", "Planning"], ["Policy Review", "Policy review"],
  ["Awaiting Approval", "Human approval"], ["Executing", "Execution"], ["Verifying", "Verification"], ["Resolved", "Resolved"]];
function pipeline(status) {
  const idx = FLOW.findIndex(([s]) => s === status);
  const extra = status === "Waiting on Customer" ? "Waiting on customer"
    : status === "Escalated" ? "Escalated" : status === "Routed" ? "Routed to a human" : null;
  return `<div class="pipeline">${FLOW.map(([s, l], i) => `<div class="pl ${status === "Resolved" || (idx >= 0 && i < idx) ? "done" : ""} ${i === idx && status !== "Resolved" ? (s === "Awaiting Approval" ? "cur human" : "cur") : ""}"><i></i>${l}</div>`).join("")}
    ${extra ? `<div class="pl cur ${status === "Escalated" ? "bad" : "human"}"><i></i>${extra}</div>` : ""}</div>`;
}

function renderCase(id) {
  view.innerHTML = `<div class="breadcrumb"><a href="#/board">Kestrel Home Support</a> / <span id="crumb"></span></div>
    <div class="case-grid"><div>
      <div class="case-title" id="title"></div><div class="case-actions" id="actions"></div>
      <div id="approval"></div>
      <div class="section"><h3>Description</h3><div class="desc" id="desc"></div></div>
      <div class="section" id="att-sec" hidden><h3>Attachments</h3><div class="attach-grid" id="atts"></div></div>
      <div class="section"><div class="tabs" id="tabs">
        <button class="tab on" data-t="work">Agent work log <em id="n-work"></em></button>
        <button class="tab" data-t="convo">Customer conversation <em id="n-convo"></em></button>
        <button class="tab" data-t="notes">Work notes <em id="n-notes"></em></button></div>
        <div id="t-work" class="worklog"><div class="muted small">Waiting for agents…</div></div>
        <div id="t-convo" hidden><div class="convo" id="convo"></div>
          <div class="reply"><textarea id="reply" placeholder="Reply to the customer, or post an internal note…"></textarea>
          <div class="r-bar"><button class="btn primary sm" id="send-cust">Send to customer</button><button class="btn sm" id="send-note">Add internal note</button><span class="spacer"></span><button class="btn sm" id="as-cust" title="Post as the customer (demo)">Reply as customer</button></div></div></div>
        <div id="t-notes" hidden class="convo"></div>
      </div></div>
      <div id="side"></div></div>`;
  let after = 0, cache = {}, lastData = null;
  const put = (k, el, html) => { if (cache[k] !== html) { cache[k] = html; el.innerHTML = html; } };
  view.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
    view.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
    ["work", "convo", "notes"].forEach((k) => ($(`#t-${k}`).hidden = k !== t.dataset.t));
  }));
  const post = async (sender) => {
    const body = $("#reply").value.trim(); if (!body) return;
    const fd = new FormData(); fd.append("body", body); fd.append("sender", sender);
    if (sender === "agent") fd.append("author", "Support agent");
    const r = await api(`/api/cases/${id}/messages`, { method: "POST", body: fd });
    $("#reply").value = ""; toast(r.resumed ? "Customer replied - agents resumed" : "Posted"); refresh();
  };
  $("#send-cust").onclick = () => post("agent"); $("#send-note").onclick = () => post("note"); $("#as-cust").onclick = () => post("customer");

  async function refresh() {
    const d = await api(`/api/cases/${id}`); lastData = d;
    const c = d.case;
    $("#crumb").textContent = c.number;
    put("title", $("#title"), esc(c.subject));
    put("actions", $("#actions"), `<span class="status-btn" style="background:var(--bg-neutral)">${lz(c.status)}</span>
      <span class="status-btn" style="background:var(--bg-neutral)">${avatar(c.assignee, d.running ? "working" : "")} ${esc(c.assignee)}</span>
      ${d.running ? `<span class="small" style="color:var(--brand);display:inline-flex;gap:6px;align-items:center"><span class="typing"><i></i><i></i><i></i></span> agents working</span>` : ""}
      <span class="spacer"></span>
      ${c.status === "Waiting on Customer" && c.scenario === "ambiguous" ? `<button class="btn" onclick="scriptedReply(${id})">${icon("user")} Simulate customer reply</button>` : ""}
      <button class="btn" onclick="runAgents(${id})">${icon("refresh")} Run agents</button>`);
    put("desc", $("#desc"), esc(c.description));
    $("#att-sec").hidden = !d.attachments.length;
    put("atts", $("#atts"), d.attachments.map((a) => `<a class="attach" href="#" onclick="viewAttachment(${a.id}, '${esc(a.content_type)}', '${esc(a.filename)}');return false">
      <div class="thumb">${a.content_type.startsWith("image/") ? `<img src="/api/attachments/${a.id}" alt="">` : icon("file", 22)}</div><div class="a-name">${esc(a.filename)}</div></a>`).join(""));
    const convo = d.messages.filter((m) => !m.internal), notes = d.messages.filter((m) => m.internal);
    $("#n-convo").textContent = convo.length; $("#n-notes").textContent = notes.length;
    put("convo", $("#convo"), convo.map((m) => {
      const ch = (m.meta || {}).choices || [];
      return `<div class="msg ${m.sender === "agent" ? "me" : ""}">${avatar(m.sender === "customer" ? "Customer" : m.author)}
      <div class="bubble"><div class="who">${esc(m.author)} <span>· ${ago(m.created_at)}</span>${(m.meta || {}).triage ? ` <span class="label">triage Q${esc((m.meta || {}).round || "")}</span>` : ""}</div>${md(m.body)}
      ${ch.length ? `<div class="offered"><span>Offered:</span>${ch.map((c) => `<span class="label">${esc(c)}</span>`).join("")}</div>` : ""}</div></div>`;
    }).join(""));
    put("notes", $("#t-notes"), notes.map((m) => `<div class="msg note">${avatar(m.author)}<div class="bubble"><div class="who">${esc(m.author)} <span>· ${ago(m.created_at)}</span></div>${md(m.body)}</div></div>`).join("") || `<div class="muted small">No notes yet.</div>`);
    put("approval", $("#approval"), approvalBanner(d));
    put("side", $("#side"), sidePanel(d));
    bindApproval(id);
    const evs = await api(`/api/cases/${id}/events?after=${after}`);
    if (evs.length) {
      if (after === 0) $("#t-work").innerHTML = "";
      evs.forEach(addEvent); after = evs[evs.length - 1].id;
      $("#n-work").textContent = $("#t-work").children.length;
    }
  }
  every(1200, refresh);
}

/* The guardrail moment: a full-width banner, not a side note - a human is being asked to
   authorise real money, and the UI should say so. */
function approvalBanner(d) {
  const p = d.proposal;
  if (!p || p.status !== "pending") return "";
  const plan = p.plan.plan;
  return `<div class="approval-banner">
    <div class="ab-head">${icon("shield", 18)} Human approval required before this plan can run</div>
    <div class="ab-reason">${p.reasons.map(esc).join("<br>")}</div>
    <div class="ab-box"><h4>Actions awaiting approval</h4>
      ${plan.actions.map((a) => `<div class="act"><span class="a-name">${esc(a.action)}</span>
        <span class="a-params">${esc(JSON.stringify(a.params))}</span></div>`).join("")}</div>
    <div class="ab-box"><h4>Why the agent chose this</h4>
      <div>${esc(plan.summary)}</div>
      ${plan.actions.map((a) => `<div class="small muted" style="margin-top:4px">${esc(a.rationale)}</div>`).join("")}
      ${plan.policy_refs && plan.policy_refs.length
        ? `<div style="margin-top:8px;display:flex;gap:5px;flex-wrap:wrap">${plan.policy_refs.map((r) => `<span class="label mono">${esc(r)}</span>`).join("")}</div>` : ""}</div>
    <div class="ab-bar">
      <input id="ap-name" value="Supervisor" placeholder="Your name">
      <input id="ap-comment" placeholder="Add a comment for the audit trail (optional)">
      <button class="btn success" id="ap-yes">${icon("check")} Approve and execute</button>
      <button class="btn danger" id="ap-no">Reject and escalate</button>
    </div></div>`;
}

function sidePanel(d) {
  const c = d.case, p = d.proposal, v = d.verification;
  let html = "";
  html += `<div class="panel"><div class="panel-head">Details</div><div class="panel-body"><dl class="dl">
    <dt>Assignee</dt><dd>${avatar(c.assignee)} ${esc(c.assignee)}</dd>
    <dt>Customer</dt><dd>${esc(c.customer_name)}<span class="muted small">${esc(c.customer_email)}</span></dd>
    <dt>Priority</dt><dd>${prio(c.priority)}</dd>
    <dt>Goal</dt><dd>${c.goal ? `<span class="label">${esc(c.goal.replace(/_/g, " "))}</span>` : "—"}</dd>
    <dt>Order</dt><dd>${c.order_id ? `<a href="#" onclick="showOrder('${esc(c.order_id)}');return false" class="mono">${esc(c.order_id)}</a>` : "—"}</dd>
    <dt>Need by</dt><dd>${esc(c.need_by || "—")}</dd>
    <dt>Channel</dt><dd>${esc(c.channel)}</dd>
    <dt>Resolution</dt><dd>${esc((c.resolution_type || "—").replace(/_/g, " "))}</dd>
    <dt>Created</dt><dd>${new Date(c.created_at).toLocaleString()}</dd></dl></div></div>`;
  html += `<div class="panel"><div class="panel-head">Agent pipeline</div><div class="panel-body">${pipeline(c.status)}</div></div>`;
  if (v) html += `<div class="panel"><div class="panel-head" style="color:${v.passed ? "var(--ok)" : "var(--warn)"}">${icon(v.passed ? "check" : "warn")} Verification · systems of record</div><div class="panel-body"><ul class="checks">
    ${v.checks.map((k) => `<li><span class="ic ${k.passed ? "ok" : "no"}">${k.passed ? "✓" : "✗"}</span><div><b>${esc(k.check.replace(/_/g, " "))}</b><div class="small muted">${esc(k.detail)}</div></div></li>`).join("")}</ul></div></div>`;
  if (p && p.status !== "pending") html += `<div class="panel"><div class="panel-head">Approval history</div><div class="panel-body small">${esc(p.status)} by ${esc(p.decided_by || "")} ${ago(p.decided_at)}${p.comment ? ` - "${esc(p.comment)}"` : ""}</div></div>`;
  return html;
}
function bindApproval(id) {
  const yes = $("#ap-yes"); if (!yes || yes._b) return; yes._b = true;
  const go = async (approve) => {
    try { await api(`/api/cases/${id}/decision`, { method: "POST", json: { approve, approver: $("#ap-name").value || "Supervisor", comment: $("#ap-comment").value } }); toast(approve ? "Approved - executing" : "Rejected - escalated"); }
    catch (e) { toast(e.message); }
  };
  yes.onclick = () => go(true); $("#ap-no").onclick = () => go(false);
}
async function runAgents(id) { const r = await api(`/api/cases/${id}/run`, { method: "POST" }); toast(r.started ? "Agents started" : "Already running"); }
async function scriptedReply(id) { await api(`/api/cases/${id}/scripted-reply`, { method: "POST" }); toast("Customer replied - agents resumed"); }
function viewAttachment(aid, type, name) {
  modal(`<header><h2>${esc(name)}</h2><span class="spacer"></span><a class="btn subtle" href="/api/attachments/${aid}" target="_blank">Open ↗</a></header><div class="m-body">
    ${type.startsWith("image/") ? `<img class="img-view" src="/api/attachments/${aid}">` : `<iframe src="/api/attachments/${aid}" style="width:100%;height:60vh;border:1px solid var(--border);border-radius:4px"></iframe>`}</div>`);
}
async function showOrder(oid) {
  const s = await api("/api/systems");
  const o = s.orders.find((x) => x.id === oid) || {};
  const ship = s.shipments.filter((x) => x.order_id === oid), ref = s.refunds.filter((x) => x.order_id === oid);
  modal(`<header><h2>${esc(oid)}</h2></header><div class="m-body"><dl class="dl">
    <dt>Customer</dt><dd>${esc(o.customer)}</dd><dt>Status</dt><dd>${esc(o.status)}</dd><dt>Placed</dt><dd>${esc(o.placed_at)}</dd>
    <dt>Delivered</dt><dd>${esc(o.delivered_at || "—")}</dd><dt>Total</dt><dd>${money(o.total)} (captured ${money(o.captured)}, refunded ${money(o.refunded)})</dd>
    <dt>Shipments</dt><dd>${ship.map((x) => `${esc(x.id)} ${esc(x.kind)} · ${esc(x.status)}`).join("<br>") || "—"}</dd>
    <dt>Refunds</dt><dd>${ref.map((x) => `${esc(x.id)} ${money(x.amount)} · ${esc(x.status)}`).join("<br>") || "—"}</dd></dl></div>
    <footer><a class="btn" href="#/systems" onclick="closeModal()">Open enterprise systems</a></footer>`);
}

// ---------- work log rendering ----------
function renderOutput(agent, d) {
  if ("goal" in d && "summary" in d && "priority" in d) return `<div class="o-title">Goal: ${esc(d.goal.replace(/_/g, " "))} ${prio(d.priority)}</div>
    <div class="small">${esc(d.summary)}</div>${d.order_id ? `<div class="small muted">order ${esc(d.order_id)} · sku ${esc(d.sku || "?")}${d.need_by ? ` · need by ${esc(d.need_by)}` : ""}</div>` : ""}
    ${d.clarifying_question ? `<div class="small" style="margin-top:4px">❓ Missing ${esc(d.missing_info.join(", "))} → asking: <i>${esc(d.clarifying_question)}</i></div>` : ""}`;
  if ("findings" in d) return `<div class="o-title">Facts & eligible options</div><ul>${d.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
    ${d.options.map((o) => `<div class="small"><span class="${o.eligible ? "opt-ok" : "opt-no"}">${o.eligible ? "✓" : "✗"}</span> <b>${esc(o.option)}</b> <span class="muted">${esc(o.policy_ref || "")}</span> - ${esc(o.reason)}</div>`).join("")}
    ${d.risk_flags.length ? `<div class="small opt-no" style="margin-top:4px">⚠ ${d.risk_flags.map(esc).join("; ")}</div>` : ""}<div class="small" style="margin-top:4px">Recommended: <b>${esc(d.recommended)}</b></div>`;
  if ("decision" in d) return `<div class="o-title">${d.decision === "execute" ? "Plan" : d.decision === "escalate" ? "Escalate" : "Inform only"} · ${esc((d.resolution_type || "").replace(/_/g, " "))}</div><div class="small">${esc(d.summary)}</div>
    ${d.actions.map((a) => `<div class="small">• <code>${esc(a.action)}</code> ${esc(JSON.stringify(a.params))}</div>`).join("")}${d.escalation_reason ? `<div class="small">${esc(d.escalation_reason)}</div>` : ""}`;
  if ("verdict" in d) return `<div class="o-title">${d.verdict === "approve" ? "✓ Approved" : "↩ Revision requested"}</div><div class="small">${esc(d.feedback)}</div>`;
  if ("customer_message" in d) return `<div class="o-title">Reply to customer</div><div class="small">${esc(d.customer_message)}</div>`;
  return `<pre class="small">${esc(JSON.stringify(d, null, 2))}</pre>`;
}
function addEvent(e) {
  const log = $("#t-work"); if (!log) return;
  const p = e.payload, a = who(e.agent);
  const row = (body, extra = "") => { const div = document.createElement("div"); div.className = "wl"; div.style.setProperty("--c", a.c);
    div.innerHTML = `${avatar(e.agent)}<div><div class="w-head"><b>${esc(e.agent)}</b>${extra}<span class="t">${ago(e.created_at)}</span></div>${body}</div>`; log.appendChild(div); return div; };
  if (e.type === "stage") {
    const div = document.createElement("div"); const t = p.text || "";
    div.className = "wl stage" + (/^(Adapting|Guardrail|Plan sent back|Verification failed|Escalated)/.test(t) || /transient/.test(t) ? " adapt" : "");
    div.innerHTML = `<div class="s">${esc(t)}</div>`; log.appendChild(div); return;
  }
  if (e.type === "tool_result") {
    const t = [...log.querySelectorAll(`details.tool[data-tool="${CSS.escape(p.tool)}"]`)].reverse().find((x) => !x.dataset.done);
    const err = p.result && typeof p.result === "object" && !Array.isArray(p.result) && p.result.error;
    if (t) { t.dataset.done = 1; const st = t.querySelector(".st"); st.className = "st " + (err ? "err" : "ok"); st.textContent = err ? `✗ ${p.result.error}` : "✓ ok";
      t.querySelector("pre").textContent = JSON.stringify(p.result, null, 2); if (err) t.open = true; }
    return;
  }
  if (e.type === "tool_call") {
    const args = Object.entries(p.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
    const last = log.lastElementChild;
    const html = `<details class="tool" data-tool="${esc(p.tool)}"><summary>${esc(p.tool)}(${esc(args.slice(0, 110))})<span class="st run">running…</span></summary><pre></pre></details>`;
    if (last && last.dataset.agent === e.agent && last.classList.contains("wl") && !last.classList.contains("stage")) { last.lastElementChild.insertAdjacentHTML("beforeend", html); return; }
    const div = row(html, " <span class='muted small'>used tools</span>"); div.dataset.agent = e.agent; return;
  }
  if (e.type === "output") { row(`<div class="out">${renderOutput(e.agent, p.data)}</div>`, `<span class="prov">${esc(p.provider || "")}</span>`); return; }
  if (e.type === "verification") { row(`<div class="out"><div class="o-title">${p.passed ? "✓ All checks passed" : "✗ Checks failed"}</div>${p.checks.map((k) => `<div class="small"><span class="${k.passed ? "opt-ok" : "opt-no"}">${k.passed ? "✓" : "✗"}</span> ${esc(k.check)} - ${esc(k.detail)}</div>`).join("")}</div>`); return; }
  if (e.type === "fallback") { row(`<div class="world fallback">${icon("warn", 16)}<div><b>Model unavailable — deterministic rules used.</b> ${esc(p.text || "")}</div></div>`); return; }
  if (e.type === "world_event") { row(`<div class="world">${icon("warn", 16)}<div><b>Environment changed:</b> ${esc(p.text)}</div></div>`); return; }
  if (e.type === "approval_required") { row(`<div class="out" style="--c:var(--warn)"><div class="o-title">Paused for human approval</div><div class="small">${p.reasons.map(esc).join("<br>")}</div></div>`); return; }
  if (e.type === "approval") { row(`<div class="out" style="--c:var(--ok)"><div class="o-title">${p.approved ? "Approved" : "Rejected"}</div><div class="small">${esc(p.comment || "")}</div></div>`); return; }
  if (e.type === "escalation") { row(`<div class="out" style="--c:var(--bad)"><div class="o-title">Escalated to ${esc(p.escalate_to)}</div><div class="small">${esc(p.escalation_reason)}</div></div>`); return; }
  if (e.type === "thought") { row(`<div class="small muted"><i>${esc(p.text)}</i></div>`); return; }
  row(`<div class="small">${esc(p.text || JSON.stringify(p))}</div>`);
}

// ---------- enterprise systems ----------
let sysTab = "orders";
function renderSystems() {
  const T = { orders: "Orders", refunds: "Payments · refunds", shipments: "Shipping", inventory: "Inventory", store_credit: "Store credit", world_events: "World events", audit: "Audit log" };
  view.innerHTML = `<div class="breadcrumb">Kestrel Home / Enterprise sandbox</div><div class="page-head"><h1>Enterprise systems</h1><div class="spacer"></div><span class="muted small">Every agent action changes these records - the verifier checks them, not the agent's words.</span></div>
    <div class="sys-tabs">${Object.entries(T).map(([k, l]) => `<button class="chip-toggle ${k === sysTab ? "on" : ""}" data-k="${k}">${l}</button>`).join("")}</div><div class="list-wrap" id="sys"></div>`;
  view.querySelectorAll("[data-k]").forEach((b) => (b.onclick = () => { sysTab = b.dataset.k; renderSystems(); }));
  const recent = (ts) => ts && Date.now() - new Date(ts).getTime() < 90000;
  const table = (cols, rows, flash = () => false) => `<table class="list"><thead><tr>${cols.map((c) => `<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map((r) =>
    `<tr class="${flash(r) ? "flash" : ""}">${cols.map((c) => `<td class="${c[2] || ""}">${c[1](r)}</td>`).join("")}</tr>`).join("") || `<tr><td class="muted" colspan="${cols.length}" style="padding:20px">No records</td></tr>`}</tbody></table>`;
  every(2500, async () => {
    const s = await api("/api/systems");
    const V = {
      orders: () => table([["Order", (r) => `<b>${esc(r.id)}</b>`], ["Customer", (r) => esc(r.customer)], ["Status", (r) => esc(r.status)], ["Placed", (r) => esc(r.placed_at)], ["Delivered", (r) => esc(r.delivered_at || "")], ["Region", (r) => esc(r.region)], ["Total", (r) => money(r.total)], ["Refunded", (r) => money(r.refunded)]], s.orders),
      refunds: () => table([["Refund", (r) => `<b>${esc(r.id)}</b>`], ["Order", (r) => esc(r.order_id)], ["Amount", (r) => money(r.amount)], ["Reason", (r) => esc(r.reason), "wrap"], ["Status", (r) => esc(r.status)], ["Idempotency key", (r) => `<span class="mono">${esc(r.idempotency_key)}</span>`], ["Created", (r) => ago(r.created_at)]], s.refunds, (r) => recent(r.created_at)),
      shipments: () => table([["Shipment", (r) => `<b>${esc(r.id)}</b>`], ["Order", (r) => esc(r.order_id)], ["Kind", (r) => esc(r.kind)], ["Status", (r) => esc(r.status)], ["SKU", (r) => esc(r.sku)], ["From", (r) => esc(r.warehouse_id || "")], ["ETA", (r) => esc(r.eta || "")], ["Proof", (r) => esc(r.proof || "")]], s.shipments, (r) => recent(r.created_at)),
      inventory: () => table([["SKU", (r) => `<b>${esc(r.sku)}</b>`], ["Product", (r) => esc(r.name)], ["Warehouse", (r) => esc(r.warehouse_id)], ["On hand", (r) => r.on_hand], ["Reserved", (r) => r.reserved], ["Available", (r) => `<b>${r.on_hand - r.reserved}</b>`]], s.inventory, (r) => r.on_hand - r.reserved <= 0),
      store_credit: () => table([["Credit", (r) => `<b>${esc(r.id)}</b>`], ["Customer", (r) => esc(r.customer_id)], ["Amount", (r) => money(r.amount)], ["Reason", (r) => esc(r.reason), "wrap"], ["Case", (r) => esc(r.case_ref)], ["Created", (r) => ago(r.created_at)]], s.store_credit, (r) => recent(r.created_at)),
      world_events: () => table([["#", (r) => r.id], ["Trigger", (r) => `<span class="mono">${esc(r.trigger)}</span>`], ["Description", (r) => esc(r.description), "wrap"], ["Fired", (r) => (r.fired ? `✓ ${ago(r.fired_at)}` : "pending")]], s.world_events),
      audit: () => table([["Time", (r) => ago(r.ts)], ["System", (r) => esc(r.system)], ["Action", (r) => `<b>${esc(r.action)}</b>`], ["Detail", (r) => `<span class="mono">${esc(r.detail)}</span>`, "wrap"], ["Kind", (r) => esc(r.kind)]], s.audit, (r) => r.kind === "world_event" || r.kind === "failure"),
    };
    $("#sys").innerHTML = V[sysTab]();
  });
}

// ---------- knowledge ----------
async function renderKnowledge() {
  const k = await api("/api/knowledge");
  view.innerHTML = `<div class="breadcrumb">Kestrel Home / Knowledge</div><div class="page-head"><h1>Policies & learned memory</h1></div>
    <h2>Learned from resolved cases</h2><div class="cards" style="margin-bottom:24px">${k.learned.map((x) => `<div class="kcard"><span class="lozenge lz-done">learned · ${esc(x.case_number)}</span><h4>${esc(x.title)}</h4><p><b>Situation:</b> ${esc(x.situation)}</p><p><b>Resolution:</b> ${esc(x.resolution)}</p><p class="small muted">${esc(x.tags)}</p></div>`).join("") || `<div class="muted small">Nothing yet - resolve a case and the Communicator Agent records what worked.</div>`}</div>
    <h2>Policy handbook (retrieved by the agents)</h2><div class="cards">${k.policies.map((x) => `<div class="kcard"><span class="lozenge lz-progress">${esc(x.id)}</span><h4>${esc(x.title)}</h4><p>${esc(x.text)}</p></div>`).join("")}</div>`;
}

route();
