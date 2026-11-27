// Threadline's one screen: your projects and what's inside them, where you stopped, and the thread of chats across
// agents.
// Your history never goes to the page's host: the page asks `threadline open` on your own computer for it.
// Without Threadline running, it shows invented sample projects.
import { LOCAL, el, explain, deviceState, downloadNano, onAnswer, askStream, warmUp, looksPrivate } from "./ai.js";

// `threadline open` passes its secret token and port in the address fragment, which never reaches any server.
function takeFragment() {
  const passed = new URLSearchParams(location.hash.slice(1));
  if (!passed.get("t")) return false;
  sessionStorage.setItem("threadline-token", passed.get("t"));
  sessionStorage.setItem("threadline-port", passed.get("p") || "");
  return true;
}
takeFragment();
history.replaceState(null, "", location.pathname);
addEventListener("hashchange", () => { if (takeFragment()) location.replace(location.pathname); });
const token = sessionStorage.getItem("threadline-token") || "";
const port = sessionStorage.getItem("threadline-port") || "";
// Served by Threadline itself: same address. Hosted: Threadline on this computer, if it passed its port.
const BASE = LOCAL ? "" : port ? `http://localhost:${port}` : null;

const AGENTS = { claude: "Claude Code", codex: "Codex", cursor: "Cursor", gemini: "Gemini CLI", zed: "Zed", copilot: "GitHub Copilot",
  copilot_cli: "Copilot CLI", vscode: "VS Code", cline: "Cline", roo: "Roo Code", kilo: "Kilo Code", kilo_cli: "Kilo CLI",
  opencode: "OpenCode", muse: "Muse Code" };
const agentName = (source) => AGENTS[source] || (source ? source[0].toUpperCase() + source.slice(1) : "an agent");
const COMPUTER = /Mac/.test(navigator.userAgent) ? "your Mac" : /Windows/.test(navigator.userAgent) ? "your PC" : "this computer";

let demo = null;
let ticket = 0;  // Bumped when another project opens, so slow answers for the old one are dropped.
const $ = (id) => document.getElementById(id);

// ---------- History: from Threadline on this computer, or the sample projects ----------

async function api(path, body) {
  if (demo) return demoApi(path, body);
  const response = await fetch(BASE + path, {
    method: body ? "POST" : "GET",
    headers: { "X-Threadline-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

// The sample projects answer the same requests the real server does.
function demoApi(path, body) {
  const url = new URL(path, location.origin);
  const q = (key) => url.searchParams.get(key) || "";
  const top = demo.projects.find((p) => p.project === q("project"));
  const inPlace = (p, place) => p.chats.filter((c) => !place || c.place === place).sort((a, b) => b.updated.localeCompare(a.updated));
  const item = (c) => ({ home: c.home, source: c.source, id: c.id, place: c.place || "", title: c.title, updated: c.updated, ending: c.exchanges.at(-1).agent, subtasks: 0 });
  if (url.pathname === "/api/projects") {
    return { projects: demo.projects.map((p) => {
      const children = {};
      for (const c of p.chats.filter((c) => c.place)) {
        const child = children[c.place] ||= { place: c.place, name: c.place === "*several" ? "Across several" : c.place, chats: 0, updated: c.updated };
        child.chats++; if (c.updated > child.updated) child.updated = c.updated;
      }
      return { project: p.project, name: p.project.split("/").pop(), chats: p.chats.length, updated: inPlace(p, "")[0].updated,
               children: Object.values(children).sort((a, b) => (a.place === "*several") - (b.place === "*several") || b.updated.localeCompare(a.updated)) };
    }) };
  }
  if (url.pathname === "/api/project") {
    const chats = inPlace(top, q("place"));
    const ids = new Set(chats.map((c) => c.id));
    const recent = chats.slice(0, 3).flatMap((c, n) => c.exchanges.slice(n ? -1 : -3).reverse().map((x) => ({ ...x, source: c.source })));
    const name = top.project.split("/").pop();
    const placeName = q("place") === "*several" ? "Across several" : q("place");
    return { project: top.project, place: q("place"), name: placeName || name, label: placeName ? `${name} / ${placeName}` : name,
             chats: chats.length, agents: [...new Set(chats.map((c) => c.source))],
             latest_request: recent[0] && { source: recent[0].source, timestamp: recent[0].timestamp, text: recent[0].user },
             recent_exchanges: recent,
             thread: chats.slice(0, 8).map(item), thread_total: chats.length };
  }
  if (url.pathname === "/api/thread") return { thread: inPlace(top, q("place")).slice(Number(q("offset"))).map(item) };
  if (url.pathname === "/api/chat") {
    const chat = demo.projects.flatMap((p) => p.chats).find((c) => c.home === q("home") && c.id === q("id"));
    return { title: chat.title, source: chat.source, exchanges: chat.exchanges.slice(-Number(q("count") || 2)).map((x) => ({ ...x, source: chat.source })) };
  }
  if (url.pathname === "/api/day") {
    const all = demo.projects.map((p) => ({ project: p.project, name: p.project.split("/").pop(),
      exchanges: p.chats.flatMap((c) => c.exchanges.map((x) => ({ ...x, source: c.source }))).sort((a, b) => a.timestamp.localeCompare(b.timestamp)) }));
    const newest = Math.max(...all.flatMap((p) => p.exchanges.map((x) => Date.parse(x.timestamp))));
    return { projects: all.map((p) => ({ ...p, exchanges: p.exchanges.filter((x) => newest - Date.parse(x.timestamp) <= 30 * 3600 * 1000) }))
      .filter((p) => p.exchanges.length).sort((a, b) => b.exchanges.at(-1).timestamp.localeCompare(a.exchanges.at(-1).timestamp)) };
  }
  if (url.pathname === "/api/timeline") {
    const all = inPlace(top, q("place")).flatMap((c) => c.exchanges.map((x) => ({ ...x, source: c.source })));
    return { exchanges: all.sort((a, b) => a.timestamp.localeCompare(b.timestamp)).slice(-60) };
  }
  if (url.pathname === "/api/search") {
    const words = q("q").toLowerCase().split(/\s+/).filter((w) => w.length > 1);
    const results = [];
    for (const c of inPlace(top, "")) {
      for (const x of c.exchanges) {
        for (const [who, text] of [["you", x.user], ["agent", x.agent]]) {
          if (words.some((w) => text.toLowerCase().includes(w))) {
            results.push({ home: c.home, source: c.source, session_id: c.id, title: c.title, timestamp: x.timestamp, who, snippet: text });
          }
        }
      }
    }
    return { results: results.slice(0, 20) };
  }
  throw new Error("Unknown request");
}

const store = {
  get(key) { try { return JSON.parse(localStorage.getItem("threadline:" + key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem("threadline:" + key, JSON.stringify(value)); } catch { /* Storage full or off. */ } },
};

function ago(timestamp) {
  const seconds = (Date.now() - Date.parse(timestamp)) / 1000;
  if (!isFinite(seconds)) return "";
  if (seconds < 90) return "just now";
  const units = [[60, "minute"], [24, "hour"], [7, "day"], [4.35, "week"], [12, "month"], [Infinity, "year"]];
  let value = seconds / 60;
  for (const [size, name] of units) {
    if (value < size) { const n = Math.round(value); return `${n} ${name}${n === 1 ? "" : "s"} ago`; }
    value /= size;
  }
  return "";
}

// ---------- Which Gemini is answering ----------

const pill = { state: "missing", lastOnDevice: null };

function showPill() {
  const node = $("model");
  const make = (cls, text, attrs = {}) => {
    const fresh = el(attrs.onclick ? "button" : "span", { class: `pill ${cls}`, id: "model", type: attrs.onclick ? "button" : null, ...attrs },
                     el("span", { class: "dot" }), text);
    node.replaceWith(fresh);
  };
  const onDevice = pill.lastOnDevice ?? pill.state === "available";
  if (pill.state === "downloading") return make("busy", pill.progress != null ? `Downloading Gemini Nano · ${Math.round(pill.progress * 100)}%` : "Downloading Gemini Nano…",
                                                { title: "Google's cloud answers until the download finishes." });
  if (pill.state === "downloadable" && !pill.lastOnDevice) {
    return make("cloud", el("span", {}, "Gemini · cloud", el("span", { class: "long" }, " · Download Gemini Nano")), {
      title: `Download Gemini Nano so your history is read on ${COMPUTER}`, onclick: getNano });
  }
  if (onDevice) return make("device", el("span", {}, "Gemini Nano", el("span", { class: "long" }, ` · on ${COMPUTER}`)),
                            { title: "Gemini Nano runs inside Chrome. Nothing you read here leaves this computer." });
  return make("cloud", el("span", {}, "Gemini", el("span", { class: "long" }, " · Google cloud")), {
    title: pill.state === "missing" ? "Gemini Nano isn't in this browser. Open this page in Chrome to read on this computer."
                                    : "Gemini Nano can't run on this computer, so Gemini in Google's cloud answers." });
}

async function getNano() {
  pill.state = "downloading"; pill.progress = 0; showPill();
  try {
    await downloadNano((fraction) => { pill.progress = fraction; showPill(); });
    pill.state = await deviceState();
  } catch { pill.state = await deviceState(); }
  pill.lastOnDevice = null;
  showPill();
}

onAnswer((answer) => { pill.lastOnDevice = answer.onDevice; showPill(); });

// ---------- Projects, and the sub-projects inside them ----------

// Each agent has its own color on the thread. Threadline's own palette: no product logos.
const COLORS = { claude: "#E39B4C", codex: "#5B9BD5", cursor: "#A98BE8", gemini: "#4FB892" };
const SPARE = ["#D46A8C", "#5FB3B3", "#C9B458", "#8C9EFF", "#E07B6A"];
function agentColor(source) {
  if (COLORS[source]) return COLORS[source];
  let hash = 0;
  for (const ch of source || "") hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return SPARE[hash % SPARE.length];
}
// A spinner with a word or two, for anything that waits on Threadline or Gemini.
const loading = (text) => el("p", { class: "loading", role: "status" }, el("span", { class: "spinner", "aria-hidden": "true" }), text);
// Shown only when a wait is long enough to notice, so fast answers don't flash.
function after(ms, show) { const timer = setTimeout(show, ms); return () => clearTimeout(timer); }

const knot = (source) => el("span", { class: "knot", style: `--c:${agentColor(source)}`, "aria-hidden": "true" });
const agentTag = (source) => el("span", { class: "agent-tag" }, knot(source), agentName(source));

let projects = [];
let current = { project: "", place: "" };

async function loadProjects() {
  ({ projects } = await api("/api/projects"));
  if (!projects.length) {
    $("projects").replaceChildren();
    $("reader").replaceChildren(el("div", {}, el("h1", {}, "No chats yet"),
      el("p", { class: "empty" }, "Use a coding agent in any project. Its chats appear here.")));
    return;
  }
  openDay();
}

function renderTree() {
  const open = new Set(store.get("open") || [current.project]);
  const row = (label, count, project, place, depth, toggle) => el("button", {
    type: "button", class: `node depth-${depth}`, "aria-current": String(current.project === project && current.place === place),
    title: place ? `${project.split("/").pop()} / ${label}` : project, onclick: () => openPlace(project, place) },
    toggle || "", el("b", {}, label), el("span", {}, count));
  const items = projects.flatMap((p) => {
    const expanded = open.has(p.project) && p.children.length;
    const chevron = p.children.length ? el("span", { class: `chevron${expanded ? " open" : ""}`, role: "button", "aria-label": expanded ? "Collapse" : "Expand",
      onclick: (event) => { event.stopPropagation(); open.has(p.project) ? open.delete(p.project) : open.add(p.project); store.set("open", [...open]); renderTree(); } }, "›")
      : el("span", { class: "chevron none" });
    return [row(p.name, ago(p.updated).replace(" ago", ""), p.project, "", 0, chevron),
            ...(expanded ? p.children.map((c) => row(c.name, c.chats, p.project, c.place, 1)) : [])];
  });
  const today = el("button", { type: "button", class: "node depth-0 day-node", "aria-current": String(!current.project), onclick: openDay },
    el("span", { class: "chevron none" }), el("b", {}, "Your day"), el("span", {}, ""));
  $("projects").replaceChildren(today, el("h2", {}, "Projects"), ...items);
}

async function openPlace(project, place) {
  const mine = ++ticket;
  current = { project, place };
  store.set("place", current);
  const open = new Set(store.get("open") || []);
  if (!open.has(project)) { open.add(project); store.set("open", [...open]); }
  renderTree();
  $("search").value = "";
  $("search").disabled = false;
  $("search").placeholder = `Search ${project.split("/").pop()}`;
  const reader = $("reader");
  const cancel = after(150, () => { if (mine === ticket) reader.replaceChildren(el("div", {}, loading(`Opening ${place ? place.split("/").pop() : project.split("/").pop()}…`))); });
  let data;
  try { data = await api(`/api/project?project=${encodeURIComponent(project)}&place=${encodeURIComponent(place)}`); } catch (error) {
    cancel();
    if (mine === ticket) reader.replaceChildren(el("div", {}, el("p", { class: "empty" }, explain(error))));
    return;
  }
  cancel();
  if (mine !== ticket) return;
  const stopped = el("section", { class: "block" });
  const thread = el("section", { class: "block" });
  const stretchesNode = el("section", { class: "block" });
  const where = data.label.includes(" / ") ? data.label + " · " : "";
  const agents = data.agents.flatMap((a, i) => [i === 0 ? " in " : i === data.agents.length - 1 ? " and " : ", ", agentTag(a)]);
  const subline = el("p", { class: "subline" }, `${where}${plural(data.chats, "chat")}`, ...agents);
  reader.replaceChildren(el("div", {}, el("h1", { title: project }, data.name), subline, stopped, stretchesNode, thread));
  reader.scrollTop = 0;
  const view = { data, mine };
  renderThread(view, thread);
  const timeline = loadTimeline(view, stretchesNode);
  await summarize(view, stopped);
  await writeTimeline(await timeline, mine);
  await writeCards([...thread.querySelectorAll(".thread-item")], mine);
}

// ---------- The thread: every chat here, newest first, drawn as one line that changes color with the agent ----------

function threadRow(chat) {
  const detail = el("div", { class: "chat-detail", hidden: true });
  const text = el("span", { class: "chat-text" }, el("b", {}, chat.title || "Untitled chat"), chat.ending ? el("span", {}, chat.ending) : null);
  const row = el("li", { class: "thread-item", "data-source": chat.source, "data-place": chat.place || "" },
    el("button", { type: "button", class: "chat-head", "aria-expanded": "false", onclick: async (event) => {
      const head = event.currentTarget;
      const opening = detail.hidden;
      detail.hidden = !opening;
      head.setAttribute("aria-expanded", String(opening));
      if (!opening || detail.childElementCount) return;
      detail.append(loading("Reading this chat…"));
      try {
        const { exchanges } = await api(`/api/chat?home=${encodeURIComponent(chat.home)}&source=${chat.source}&id=${encodeURIComponent(chat.id)}`);
        detail.replaceChildren(...exchanges.flatMap((x) => [
          x.user ? el("div", { class: "said you" }, el("span", {}, "You"), x.user) : null,
          x.agent ? el("div", { class: "said" }, el("span", {}, agentName(chat.source)), x.agent) : null]));
      } catch (error) { detail.replaceChildren(el("p", { class: "empty" }, explain(error))); }
    } },
      knot(chat.source),
      text,
      el("span", { class: "when" }, el("span", { class: "who" }, agentName(chat.source)), ago(chat.updated))),
    detail);
  row.chat = chat;
  row.showCard = (card, writing = false) => {
    if (!writing) row.dataset.card = "1";
    row.classList.toggle("typing", writing);
    text.replaceChildren(el("b", { title: chat.title || "" }, card.title), el("span", {}, card.done || ""));
  };
  const saved = store.get(cardKey(chat));
  if (saved) row.showCard(saved);
  return row;
}

// Color each stretch of the line, and mark where the work passed from one agent to another: the next chat in the same
// sub-project was in a different agent. Chats about different sub-projects are separate work, so no note is drawn.
function drawThread(list) {
  list.querySelectorAll(".handoff").forEach((node) => node.remove());
  const rows = [...list.querySelectorAll(".thread-item")];
  rows.forEach((row, i) => {
    const older = rows[i + 1];
    row.style.setProperty("--c", agentColor(row.dataset.source));
    row.style.setProperty("--next", agentColor((older || row).dataset.source));
    row.classList.toggle("first", i === 0);
    row.classList.toggle("last", !older);
    if (older && older.dataset.source !== row.dataset.source && older.dataset.place === row.dataset.place) {
      const mark = el("li", { class: "handoff", "aria-hidden": "true",
        style: `--c:${agentColor(row.dataset.source)};--next:${agentColor(older.dataset.source)}` },
        agentName(row.dataset.source), " picked up from ", agentName(older.dataset.source));
      row.after(mark);
    }
  });
}

function renderThread(view, node) {
  const { data } = view;
  const list = el("ol", { class: "thread" }, data.thread.map(threadRow));
  drawThread(list);
  let shown = data.thread.length;
  const more = data.thread_total > shown && el("button", { type: "button", class: "button small", onclick: async () => {
    more.disabled = true;
    more.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), "Loading earlier chats");
    try {
      const { thread } = await api(`/api/thread?project=${encodeURIComponent(data.project)}&place=${encodeURIComponent(data.place)}&offset=${shown}`);
      const rows = thread.map(threadRow);
      list.append(...rows);
      drawThread(list);
      writeCards(rows, view.mine);
      shown += thread.length;
      if (shown >= data.thread_total) more.remove(); else { more.disabled = false; more.textContent = `${data.thread_total - shown} earlier chats`; }
    } catch { more.disabled = false; more.textContent = `${data.thread_total - shown} earlier chats`; }
  } }, `${data.thread_total - shown} earlier chats`);
  node.replaceChildren(el("h2", { class: "label" }, "The thread"),
    data.thread.length ? list : el("p", { class: "empty" }, "No chats here yet."), more || "");
}

// ---------- Search ----------

let searchTimer = 0;
function onSearch(event) {
  clearTimeout(searchTimer);
  const words = event.target.value.trim();
  if (!words) return openPlace(current.project, current.place);
  searchTimer = setTimeout(async () => {
    const mine = ++ticket;
    const cancel = after(150, () => { if (mine === ticket) $("reader").replaceChildren(el("div", {}, loading(`Searching for “${words}”…`))); });
    let results;
    try { ({ results } = await api(`/api/search?project=${encodeURIComponent(current.project)}&q=${encodeURIComponent(words)}`)); } catch (error) {
      cancel();
      if (mine === ticket) $("reader").replaceChildren(el("div", {}, el("p", { class: "empty" }, explain(error))));
      return;
    } finally { cancel(); }
    if (mine !== ticket) return;
    const lowered = words.toLowerCase().split(/\s+/).filter((w) => w.length > 1);
    const marked = (text) => {
      const pattern = new RegExp(`(${lowered.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "ig");
      return text.split(pattern).map((part, i) => (i % 2 ? el("mark", {}, part) : part));
    };
    $("reader").replaceChildren(el("div", {},
      el("h2", { class: "label" }, results.length ? `${results.length === 20 ? "Top 20" : results.length} result${results.length === 1 ? "" : "s"}` : "No results"),
      el("ol", { class: "results" }, results.map((r) => el("li", {},
        el("div", { class: "result-head" }, knot(r.source), el("b", {}, r.title || "Untitled chat"), el("span", { class: "when" }, ago(r.timestamp))),
        el("p", {}, el("span", { class: "who" }, r.who === "you" ? "You: " : `${agentName(r.source)}: `), ...marked(r.snippet)))))));
  }, 250);
}
$("search").addEventListener("input", onSearch);
$("search").addEventListener("keydown", (event) => { if (event.key === "Escape") { event.target.value = ""; openPlace(current.project, current.place); } });

// ---------- Where you stopped ----------

const clip = (text, n) => { const t = String(text || "").replace(/<\/?pasted_content[^>]*>/g, "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "…" : t; };
// An agent's reply ends with what it did and what's left, so keep its end.
const tail = (text, n) => { const t = String(text || "").replace(/\s+/g, " ").trim(); return t.length > n ? "…" + t.slice(-n) : t; };

function summaryPrompt(data) {
  const when = (stamp) => stamp ? new Date(stamp).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "time unknown";
  const exchanges = [...(data.recent_exchanges || [])].reverse().map((x) =>
    `[${when(x.timestamp)}, ${agentName(x.source)}]\nDeveloper: ${clip(x.user, 400)}\nAgent: ${x.agent ? tail(x.agent, 700) : "(no reply yet: the agent is still working on this)"}`).join("\n\n");
  return `You help a developer pick up their work in the project "${data.name}", done with AI coding agents.
Write 2 or 3 short sentences in second person ("You were ...") saying where the work stopped: what was being done and what is unfinished or next.
The exchanges are in time order and the LAST one is the newest: describe that one. Earlier exchanges are only background; never present older work as current, and never call something next if a later exchange shows it was done.
Wrap the single most important unfinished thing in [[double brackets]]. Plain words. No greeting, no list, no markdown.

Latest exchanges, oldest first (the last one is where the work is now):
${exchanges || "(none)"}

The developer's last message: ${clip(data.latest_request?.text, 400) || "(none)"}`;
}

function summaryText(text) {
  let clean = text.replace(/\*\*|__|^#+\s*/gm, "").trim();
  // While it's still being written: close an open [[ so the underline shows, and hide a half-typed bracket.
  clean = clean.replace(/(?<![[\]])[[\]]$/, "");
  if ((clean.match(/\[\[/g) || []).length > (clean.match(/\]\]/g) || []).length) clean += "]]";
  return clean.split(/\[\[(.+?)\]\]/s).map((part, i) => (i % 2 ? el("u", {}, part) : part));
}

const LOCK = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5" fill="currentColor"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>';
function privateNote(text) {
  const note = el("p", { class: "private-note" }, text);
  note.insertAdjacentHTML("afterbegin", LOCK);
  return note;
}
const isPrivate = (x) => looksPrivate(x.user) || looksPrivate(x.agent);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Chats that look like they hold a password or key are only read by Gemini Nano on this computer. Without Gemini
// Nano, they are left out of what goes to the cloud, and the page says so.
function privacyPlan(data) {
  const exchanges = data.recent_exchanges || [];
  const held = exchanges.filter(isPrivate).length + (looksPrivate(data.latest_request?.text) ? 1 : 0);
  if (!held) return { data, mode: undefined, note: null, tag: "" };
  if (pill.state === "available") {
    return { data, mode: "ONLY_ON_DEVICE", tag: "device",
             note: `Read only by Gemini Nano on ${COMPUTER}: these chats look like they contain a password or key.` };
  }
  const kept = exchanges.filter((x) => !isPrivate(x));
  return { data: { ...data, recent_exchanges: kept, latest_request: looksPrivate(data.latest_request?.text) ? null : data.latest_request },
           mode: "ONLY_IN_CLOUD", tag: "held", empty: !kept.length,
           note: `Left out ${plural(held, "message")} that look${held === 1 ? "s" : ""} like ${held === 1 ? "it contains" : "they contain"} a password or key. Only Gemini Nano on ${COMPUTER} may read ${held === 1 ? "it" : "them"}.` };
}

// Written once per new chat activity and remembered, so opening a project again is instant. The summary is also
// given to Threadline, so the next agent you open starts from it.
async function summarize(view, node) {
  const { data, mine } = view;
  const paragraph = el("p", { class: "summary waiting" }, "Reading your latest chats", el("span", { class: "cursor" }));
  node.replaceChildren(el("h2", { class: "label" }, "Where you stopped"), paragraph);
  if (!data.latest_request && !(data.recent_exchanges || []).length) {
    paragraph.replaceChildren("Nothing to summarize yet.");
    return;
  }
  const plan = privacyPlan(data);
  if (plan.note) node.append(privateNote(plan.note));
  if (plan.empty) {
    paragraph.className = "empty";
    paragraph.replaceChildren(pill.state === "downloadable"
      ? ["Everything here looks private. ", el("button", { type: "button", class: "link", onclick: getNano }, "Download Gemini Nano"), " to summarize it on ", COMPUTER, "."]
      : "Everything here looks private, and Gemini Nano isn't available in this browser, so nothing was sent.");
    return;
  }
  const newest = (data.recent_exchanges || [])[0] || {};
  const key = `summary2:${data.project}:${data.place}:${newest.timestamp || ""}:${(newest.agent || "").length}:${plan.tag}`;
  let text = store.get(key)?.text;
  if (!text) {
    try {
      const answer = await askStream(summaryPrompt(plan.data), (partial) => {
        if (mine !== ticket) return;
        paragraph.className = "summary";
        paragraph.replaceChildren(...summaryText(partial), el("span", { class: "cursor" }));
      }, plan.mode);
      text = answer.text;
      store.set(key, { text });
      if (!demo) api("/api/summary", { project: data.project, place: data.place, text, method: answer.onDevice ? "gemini_nano" : "gemini_cloud" })
        .catch((error) => console.warn("Couldn't hand the summary to Threadline:", error));
    } catch (error) {
      if (mine !== ticket) return;
      console.warn(error);
      paragraph.className = "empty";
      paragraph.replaceChildren(explain(error), " ",
        el("button", { type: "button", class: "link", onclick: () => summarize(view, node) }, "Try again"));
      return;
    }
  }
  if (mine !== ticket) return;
  paragraph.className = "summary";
  paragraph.replaceChildren(...summaryText(text));
}

// ---------- Chat cards and the timeline, written by Gemini one at a time after "where you stopped" ----------

let line = Promise.resolve();
function inTurn(job) { const run = line.then(job, job); line = run.catch(() => {}); return run; }

// Like "where you stopped": chats that look private are read only by Gemini Nano, or left out without it.
function route(exchanges) {
  if (!exchanges.some(isPrivate)) return { exchanges, mode: undefined };
  if (pill.state === "available") return { exchanges, mode: "ONLY_ON_DEVICE" };
  const kept = exchanges.filter((x) => !isPrivate(x));
  return kept.length ? { exchanges: kept, mode: "ONLY_IN_CLOUD" } : null;
}

// Gemini answers in a fixed JSON shape (it can't do otherwise). Fields are read even from a half-written answer, so
// the words can appear as they're written.
const CARD = { type: "object", properties: { title: { type: "string" }, done: { type: "string" } }, required: ["title", "done"] };
const STRETCH = { type: "object", properties: { title: { type: "string" }, what: { type: "string" } }, required: ["title", "what"] };

function partial(text, name) {
  const match = text.match(new RegExp(`"${name}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)`));
  if (!match) return "";
  try { return JSON.parse(`"${match[1].replace(/\\+$/, "")}"`); } catch { return match[1]; }
}

// Small models drift into "the developer" and "the agent" however they're asked. This is about you, and the agent
// has a name.
function voice(text, agent) {
  return text.replace(/\b(?:the|a) (?:developer|user)'s\b/gi, "your").replace(/\b(?:the|a) (?:developer|user)\b/gi, "you")
    .replace(/\b(?:the|an?) (?:AI )?(?:coding )?(?:agent|assistant)\b/gi, agent).replace(/^\s*(\w)/, (c) => c.toUpperCase());
}

function answerFields(text, names, agent, done = true) {
  let found = null;
  if (done) try { found = JSON.parse(text); } catch { /* Read what's there. */ }
  found ||= Object.fromEntries(names.map((name) => [name, partial(text, name)]));
  const clean = Object.fromEntries(names.map((name) => [name, voice(String(found[name] || "").trim(), agent)]));
  if (clean.title) clean.title = clean.title.replace(/[.:]+$/, "");
  return !done || names.every((name) => clean[name]) ? clean : null;
}

const cardKey = (chat) => `card:${chat.source}:${chat.id}:${chat.updated}`;
const said = (exchanges, user, agent) => exchanges.map((x) =>
  `You: ${clip(x.user, user) || "(nothing)"}\n${agentName(x.source)}: ${x.agent ? tail(x.agent, agent) : "(no reply yet)"}`).join("\n\n");

function cardPrompt(chat, exchanges) {
  const agent = agentName(chat.source);
  return `Below is a chat between you, a developer, and ${agent}, an AI coding agent. Write a short card about it, talking to the developer as "you".
Answer in JSON:
"title": 3 to 7 words naming the work, like a commit title.
"done": one sentence of at most 20 words on what happened, saying who did it and naming the concrete things (files, features, numbers). Shape it like "You had ${agent} <do something> in <file>." or "${agent} <did something> after you <asked something>."
Use only what the chat says. Plain words.

The chat started with: ${clip(chat.title, 300)}

Its last exchanges, oldest first:
${said(exchanges.map((x) => ({ ...x, source: chat.source })), 300, 500)}`;
}

async function writeCards(rows, mine) {
  for (const row of rows) {
    if (mine !== ticket) return;
    if (row.dataset.card) continue;
    const chat = row.chat;
    row.classList.add("writing");
    try {
      const card = await inTurn(async () => {
        if (mine !== ticket) return null;
        const { exchanges } = await api(`/api/chat?home=${encodeURIComponent(chat.home)}&source=${chat.source}&id=${encodeURIComponent(chat.id)}&count=4`);
        const plan = route(exchanges);
        if (!plan) return null;
        const agent = agentName(chat.source), names = ["title", "done"];
        const answer = await askStream(cardPrompt(chat, plan.exchanges), (text) => {
          const soFar = answerFields(text, names, agent, false);
          if (soFar.title) row.showCard(soFar, true);
        }, plan.mode, CARD);
        const found = answerFields(answer.text, names, agent);
        return found;
      });
      if (card) { store.set(cardKey(chat), card); row.showCard(card); }
    } catch (error) {
      console.warn("Couldn't write a chat card:", error);
      return;  // The same failure would repeat for every chat.
    } finally { row.classList.remove("writing", "typing"); }
  }
}

// Stretches of work: exchanges with no more than 45 minutes between them, across every agent. Newest first.
const GAP = 45 * 60 * 1000;
function stretches(exchanges) {
  const blocks = [];
  for (const x of exchanges) {
    const at = Date.parse(x.timestamp);
    if (Number.isNaN(at)) continue;
    const last = blocks.at(-1);
    if (last && at - last.end <= GAP) { last.items.push(x); last.end = at; } else blocks.push({ start: at, end: at, items: [x] });
  }
  return blocks.reverse().slice(0, 6);
}

function span(block) {
  const day = (ms) => new Date(ms).toDateString() === new Date().toDateString() ? "Today"
    : new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  const time = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return block.end - block.start < 60000 ? `${day(block.start)}, ${time(block.start)}` : `${day(block.start)}, ${time(block.start)}–${time(block.end)}`;
}

function stretchPrompt(items) {
  return `Below are your exchanges with AI coding agents during one stretch of work. Sum it up, talking to the developer as "you".
Answer in JSON:
"title": 3 to 7 words naming the work, like a commit title.
"what": one sentence of at most 20 words, in the past tense, on the main thing that happened, saying who did it and naming the concrete things (files, features, numbers). Shape it like "You and <agent> <did something>." or "You decided <something>, and <agent> <did something>."
Use only what the exchanges say. Plain words.

The exchanges, oldest first:
${said(items, 250, 350)}`;
}

// Rows for stretches of work, each named by Gemini (or shown from what was saved). Used by a project's timeline and
// by "your day"; the same stretch keeps the same name in both.
function stretchRows(blocks, project, timesOnly = false) {
  for (const block of blocks) {
    block.key = `stretch:${project}::${block.start}:${block.end}:${block.items.length}`;
    const agents = [...new Set(block.items.map((x) => x.source))];
    block.title = el("b", { class: "pending" }, "Naming this stretch…");
    block.what = el("span", {}, plural(block.items.length, "exchange"));
    block.agent = agentName(agents.sort((a, b) => block.items.filter((x) => x.source === b).length - block.items.filter((x) => x.source === a).length)[0]);
    block.show = (found, writing = false) => {
      block.title.className = ""; block.node.classList.toggle("typing", writing);
      block.title.textContent = found.title; block.what.textContent = found.what || "";
    };
    block.node = el("li", {}, el("span", { class: "when" }, timesOnly ? span(block).split(", ").pop() : span(block)),
      el("span", { class: "stretch-text" }, block.title, block.what), el("span", { class: "knots" }, agents.map(knot)));
    const saved = store.get(block.key);
    if (saved) { block.show(saved); block.named = saved; }
  }
  return el("ol", { class: "stretches" }, blocks.map((b) => b.node));
}

async function loadTimeline(view, node) {
  const { data } = view;
  let exchanges = [];
  try { ({ exchanges } = await api(`/api/timeline?project=${encodeURIComponent(data.project)}&place=${encodeURIComponent(data.place)}`)); } catch { /* No timeline. */ }
  const blocks = stretches(exchanges);
  if (!blocks.length) { node.remove(); return []; }
  node.replaceChildren(el("h2", { class: "label" }, "Timeline"), stretchRows(blocks, data.project + (data.place ? "/" + data.place : "")));
  return blocks;
}

async function writeTimeline(blocks, mine) {
  for (const block of blocks) {
    if (mine !== ticket) return;
    if (!block.title.classList.contains("pending")) continue;
    try {
      const found = await inTurn(async () => {
        if (mine !== ticket) return null;
        // A long stretch: its start says what it was for, its end what came of it.
        const plan = route(block.items.length > 6 ? [...block.items.slice(0, 2), ...block.items.slice(-4)] : block.items);
        if (!plan) return null;
        const names = ["title", "what"];
        const answer = await askStream(stretchPrompt(plan.exchanges), (text) => {
          const soFar = answerFields(text, names, block.agent, false);
          if (soFar.title) block.show(soFar, true);
        }, plan.mode, STRETCH);
        return answerFields(answer.text, names, block.agent);
      });
      if (found) { store.set(block.key, found); block.show(found); block.named = found; }
      else if (mine === ticket) {  // Gemini didn't answer in the asked form: show what was asked instead.
        const asked = [...block.items].reverse().find((x) => x.user)?.user;
        block.show({ title: clip(asked || "Untitled stretch", 80), what: plural(block.items.length, "exchange") });
      }
    } catch (error) {
      console.warn("Couldn't name a stretch of work:", error);
      return error;
    }
  }
}

// ---------- Your day: every project you worked on in your last day of work, summed up by Gemini ----------

const HEADLINE = { type: "object", properties: { headline: { type: "string" } }, required: ["headline"] };
const dayName = (ms) => new Date(ms).toDateString() === new Date().toDateString() ? "Today"
  : new Date(ms).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
const clock = (ms) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

function headlinePrompt(rows, when) {
  const lines = rows.map((r) => `${r.name}:\n${r.blocks.filter((b) => b.named).map((b) => `- ${span(b).split(", ").pop()}, with ${[...new Set(b.items.map((x) => agentName(x.source)))].join(" and ")}: ${b.named.title}. ${b.named.what}`).join("\n")}`);
  return `Here is what a developer did ${when === "Today" ? "today" : `on ${when}`} with AI coding agents, project by project:
${lines.join("\n\n")}

Answer in JSON:
"headline": two sentences, at most 40 words, recapping the day for the developer: where most of the work went and the main things that got done, naming them. When you name an agent, use the one listed for that work. Talk to the developer as "you"; never say "we" or "I". Plain and warm, with no greeting and no exclamation marks.`;
}

async function openDay() {
  const mine = ++ticket;
  current = { project: "", place: "" };
  renderTree();
  $("search").value = "";
  $("search").disabled = true;
  $("search").placeholder = "Open a project to search it";
  const reader = $("reader");
  const cancel = after(150, () => { if (mine === ticket) reader.replaceChildren(el("div", {}, loading("Reading your day…"))); });
  let found;
  try { ({ projects: found } = await api("/api/day")); } catch (error) {
    cancel();
    // An older Threadline on this computer doesn't know "your day" yet: open the newest project instead.
    if (mine === ticket && projects.length) return openPlace(projects[0].project, "");
    if (mine === ticket) reader.replaceChildren(el("div", {}, el("p", { class: "empty" }, explain(error))));
    return;
  }
  cancel();
  if (mine !== ticket) return;
  // The day is your last day of work, in your own time zone.
  const stamps = found.flatMap((p) => p.exchanges.map((x) => Date.parse(x.timestamp))).filter((n) => !Number.isNaN(n));
  if (!stamps.length) { reader.replaceChildren(el("div", {}, el("h1", {}, "Your day"), el("p", { class: "empty" }, "No chats yet."))); return; }
  const lastDay = new Date(Math.max(...stamps)).toDateString();
  const when = dayName(Math.max(...stamps));
  const rows = found.map((p) => ({ ...p, items: p.exchanges.filter((x) => new Date(x.timestamp).toDateString() === lastDay) }))
    .filter((r) => r.items.length);
  const agents = [...new Set(rows.flatMap((r) => r.items.map((x) => x.source)))];
  for (const r of rows) {
    const times = r.items.map((x) => Date.parse(x.timestamp));
    r.blocks = stretches(r.items);
    r.node = el("li", {},
      el("button", { type: "button", class: "day-head", onclick: () => openPlace(r.project, "") },
        el("b", {}, r.name), el("span", { class: "when" }, Math.max(...times) - Math.min(...times) < 60000 ? clock(Math.min(...times)) : `${clock(Math.min(...times))}–${clock(Math.max(...times))}`)),
      stretchRows(r.blocks, r.project, true));
  }
  const headline = el("p", { class: "summary waiting" }, "Putting your day together", el("span", { class: "cursor" }));
  const subline = el("p", { class: "subline" }, `${plural(rows.length, "project")}, ${plural(rows.reduce((n, r) => n + r.items.length, 0), "exchange")}`,
    ...agents.flatMap((a, i) => [i === 0 ? " with " : i === agents.length - 1 ? " and " : ", ", agentTag(a)]));
  reader.replaceChildren(el("div", {}, el("h1", {}, when === "Today" ? "Your day" : when), subline,
    el("section", { class: "block" }, el("h2", { class: "label" }, when === "Today" ? "Today in short" : "That day in short"), headline),
    el("section", { class: "block" }, el("h2", { class: "label" }, "Where it went"), el("ol", { class: "day-list" }, rows.map((r) => r.node)))));
  reader.scrollTop = 0;

  // Name the stretches project by project, then recap the day from those names.
  for (const r of rows) {
    const failed = await writeTimeline(r.blocks, mine);
    if (mine !== ticket) return;
    if (failed) { headline.className = "empty"; headline.replaceChildren(explain(failed)); return; }
  }
  if (!rows.some((r) => r.blocks.some((b) => b.named))) { headline.className = "empty"; headline.replaceChildren("Nothing to recap yet."); return; }
  const key = `dayline:${rows.flatMap((r) => r.blocks.map((b) => b.key)).join("|")}`;
  let text = store.get(key)?.headline;
  if (!text) {
    // The names came from your chats: if any of those look private, the recap stays on Gemini Nano too.
    const privateDay = pill.state === "available" && rows.some((r) => r.items.some(isPrivate));
    try {
      text = await inTurn(async () => {
        const answer = await askStream(headlinePrompt(rows, when), (partial) => {
          const soFar = answerFields(partial, ["headline"], "your agent", false);
          if (soFar.headline && mine === ticket) { headline.className = "summary"; headline.replaceChildren(soFar.headline, el("span", { class: "cursor" })); }
        }, privateDay ? "ONLY_ON_DEVICE" : undefined, HEADLINE);
        return answerFields(answer.text, ["headline"], "your agent")?.headline || null;
      });
    } catch (error) { console.warn("Couldn't recap the day:", error); }
    if (text) store.set(key, { headline: text });
  }
  if (mine !== ticket) return;
  headline.className = text ? "summary" : "empty";
  headline.replaceChildren(text || "Couldn't recap the day.");
}

// ---------- Start ----------

async function useDemo(reason) {
  demo = await (await fetch("demo.json")).json();
  // The samples' last day of work becomes today, so they read like your own week.
  const stamps = demo.projects.flatMap((p) => p.chats.flatMap((c) => c.exchanges.map((x) => Date.parse(x.timestamp))));
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(Math.max(...stamps)).setHours(0, 0, 0, 0)) / 86400000);
  const later = (stamp) => new Date(Date.parse(stamp) + days * 86400000).toISOString();
  for (const chat of demo.projects.flatMap((p) => p.chats)) {
    chat.updated = later(chat.updated);
    chat.exchanges.forEach((x) => { x.timestamp = later(x.timestamp); });
  }
  $("notice").replaceChildren(el("span", {}, el("strong", {}, "These are sample projects. "), reason));
  $("notice").hidden = false;
}

(async () => {
  if (BASE === null || !token) {
    await useDemo(LOCAL ? ["To see your own, open the address ", el("code", {}, "threadline open"), " printed."]
                        : ["To see your own, ", el("a", { href: "/#install" }, "install Threadline"), " and run ", el("code", {}, "threadline open"), "."]);
  } else {
    try { await api("/api/projects"); } catch {
      await useDemo(LOCAL ? ["To see your own, open the address ", el("code", {}, "threadline open"), " printed."]
                          : ["Couldn't reach Threadline on this computer. Run ", el("code", {}, "threadline open"), " again, and allow the page if Chrome asks."]);
    }
  }
  pill.state = await deviceState();
  showPill();
  warmUp();  // Runs alongside loading the projects.
  try { await loadProjects(); } catch (error) { $("reader").replaceChildren(el("div", {}, el("p", { class: "empty" }, explain(error)))); }
})();
