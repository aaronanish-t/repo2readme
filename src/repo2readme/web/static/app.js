"use strict";

/* ---------------------------------------------------------------- helpers */

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
};
const svg = (paths, cls = "icon") => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", cls);
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = paths; // static, trusted markup only
  return s;
};
const ICON_CHECK = '<path d="M5 12l5 5L20 7"/>';
const ICON_X = '<path d="M18 6L6 18M6 6l12 12"/>';
const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString());
const storage = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};

let toastTimer;
function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 1800);
}

function parseRepo(value) {
  const v = value.trim().replace(/\.git$/, "").replace(/\/+$/, "");
  const m = v.match(/^(?:https?:\/\/)?(?:www\.)?github\.com\/([\w.-]+)\/([\w.-]+)$/i) || v.match(/^([\w-]+)\/([\w.-]+)$/);
  return m ? `${m[1]}/${m[2]}` : null;
}

/* ---------------------------------------------------------------- theme */

function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

$("theme-toggle").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("r2r-theme", next); } catch { /* ignore */ }
  rerenderDiagrams();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!document.documentElement.dataset.theme) rerenderDiagrams();
});

/* ---------------------------------------------------------------- server mode */

const state = {
  job: null,
  repo: null,
  markdown: "",
  report: null,
  pollTimer: null,
  tickTimer: null,
  startedAt: 0,
  finishedAt: 0,
  mode: null,
};

fetch("/healthz")
  .then((r) => r.json())
  .then((h) => {
    state.mode = h.mode;
    const badge = $("mode-badge");
    badge.textContent = h.mode === "model" ? "Claude" : "Facts-only";
    badge.style.setProperty("--c", h.mode === "model" ? "var(--ok)" : "var(--warn)");
    badge.title = h.mode === "model" ? `Model: ${h.model}` : "No API key configured: READMEs are built from extracted facts only";
    badge.hidden = false;
  })
  .catch(() => {});

/* ---------------------------------------------------------------- recent runs */

function renderRecent() {
  const items = storage.get("r2r-recent", []);
  $("recent").hidden = items.length === 0;
  $("recent-list").replaceChildren(
    ...items.slice(0, 6).map((it) =>
      el("button", { class: "chip", type: "button", onclick: () => start(it.repo) },
        it.repo, el("span", { class: "ago", text: ago(it.at) })),
    ),
  );
}
function remember(repo) {
  const items = storage.get("r2r-recent", []).filter((it) => it.repo !== repo);
  items.unshift({ repo, at: Date.now() });
  storage.set("r2r-recent", items.slice(0, 8));
}
function ago(ts) {
  const s = Math.max(1, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/* ---------------------------------------------------------------- landing */

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const repo = parseRepo($("url").value);
  if (!repo) {
    showFormError("Enter a GitHub repository like github.com/owner/repo or owner/repo.");
    return;
  }
  start(repo);
});
$("url").addEventListener("input", () => { $("form-error").hidden = true; });
document.querySelectorAll(".examples .chip").forEach((chip) =>
  chip.addEventListener("click", () => { $("url").value = `github.com/${chip.dataset.repo}`; start(chip.dataset.repo); }),
);
$("back").addEventListener("click", () => showLanding(true));
$("retry").addEventListener("click", () => state.repo && start(state.repo));
window.addEventListener("popstate", () => {
  const repo = new URLSearchParams(location.search).get("repo");
  if (repo && parseRepo(repo)) start(parseRepo(repo), { push: false });
  else showLanding(false);
});

function showFormError(message) {
  $("form-error").textContent = message;
  $("form-error").hidden = false;
  $("url").focus();
}

function showLanding(push) {
  stopTimers();
  state.job = null;
  $("workspace").hidden = true;
  $("hero").hidden = false;
  $("how").hidden = false;
  if (push) history.pushState({}, "", "/");
  document.title = "repo2readme — READMEs grounded in the code";
  renderRecent();
  window.scrollTo({ top: 0 });
}

/* ---------------------------------------------------------------- job lifecycle */

async function start(repo, { push = true } = {}) {
  stopTimers();
  const [owner, name] = repo.split("/");
  state.repo = repo;
  state.job = null;
  state.markdown = "";
  state.report = null;
  state.startedAt = Date.now();
  state.finishedAt = 0;

  if (push) history.pushState({ repo }, "", `/?repo=${encodeURIComponent(repo)}`);
  document.title = `${repo} · repo2readme`;
  $("hero").hidden = true;
  $("how").hidden = true;
  $("workspace").hidden = false;
  window.scrollTo({ top: 0 });

  $("repo-owner").textContent = owner;
  $("repo-name").textContent = name;
  $("repo-link").href = `https://github.com/${repo}`;
  $("repo-meta").replaceChildren(el("span", { text: "Queued" }));
  setStatus("running", "Starting");
  for (const id of ["share", "copy", "download"]) $(id).disabled = true;
  $("facts-notice").hidden = true;
  $("stats-card").hidden = true;
  $("issue-count").hidden = true;
  $("skeleton").hidden = false;
  $("skeleton-caption").textContent = "Sending the repository to a worker…";
  $("error-state").hidden = true;
  $("preview").hidden = true;
  $("raw").textContent = "";
  $("diagram").replaceChildren(el("p", { class: "empty", text: "The diagram appears when the README is ready." }));
  $("report").replaceChildren(el("p", { class: "empty", text: "The grounding report appears when the README is ready." }));
  selectTab("preview");
  renderTimeline(null);
  state.tickTimer = setInterval(tick, 1000);
  tick();

  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_url: `https://github.com/${repo}` }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(errorText(res.status, body));
    remember(repo);
    handle(body);
  } catch (err) {
    fail(err.message);
  }
}

function errorText(status, body) {
  const detail = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail[0]?.msg : null;
  if (status === 429) return detail || "You've hit the hourly limit for new repositories. Try again later, or pick a repository generated recently.";
  return detail || `The server returned an error (${status}).`;
}

async function poll() {
  if (!state.job) return;
  try {
    const res = await fetch(`/api/jobs/${state.job.id}`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(res.status === 404 ? "This job expired on the server (it may have restarted). Try again." : errorText(res.status, body));
    handle(body);
  } catch (err) {
    fail(err.message);
  }
}

function handle(job) {
  if (job.repo !== state.repo) return; // a newer run replaced this one
  state.job = job;
  renderTimeline(job);
  if (job.status === "done") {
    finish();
    showResult(job);
  } else if (job.status === "error") {
    fail(job.error || "Generation failed.");
  } else {
    setStatus("running", job.status === "queued" ? "Queued" : "Running");
    $("skeleton-caption").textContent = captionFor(job);
    state.pollTimer = setTimeout(poll, 1200);
  }
}

function finish() {
  state.finishedAt = Date.now();
  clearInterval(state.tickTimer);
  tick();
}

function fail(message) {
  stopTimers();
  state.finishedAt = Date.now();
  setStatus("error", "Failed");
  renderTimeline(state.job, true);
  $("skeleton").hidden = true;
  $("preview").hidden = true;
  $("error-state").hidden = false;
  $("error-message").textContent = message;
  selectTab("preview");
}

function stopTimers() {
  clearTimeout(state.pollTimer);
  clearInterval(state.tickTimer);
}

function tick() {
  const end = state.finishedAt || Date.now();
  const s = Math.max(0, Math.round((end - state.startedAt) / 1000));
  $("elapsed").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function setStatus(kind, label) {
  const pill = $("status-pill");
  pill.className = `status-pill ${kind}`;
  pill.textContent = label;
}

/* ---------------------------------------------------------------- pipeline timeline */

const PIPELINE = [
  { key: "fetch", label: "Clone repository", stages: ["fetch"] },
  { key: "parse", label: "Parse with tree-sitter", stages: ["walk", "parse"], unit: "files" },
  { key: "plan", label: "Plan chunks", stages: ["plan"] },
  { key: "map", label: "Read files", stages: ["map"], unit: "chunks", model: true },
  { key: "reduce", label: "Summarize modules", stages: ["reduce_modules"], unit: "batches", model: true },
  { key: "synthesize", label: "Write README", stages: ["synthesize"] },
  { key: "repair", label: "Verify and repair", stages: ["repair"], model: true },
];

function stepStates(job, failed) {
  const events = new Map((job?.stages || []).map((s) => [s.stage, s]));
  const finished = job?.status === "done";
  const seenIdx = PIPELINE.map((p, i) => (p.stages.some((s) => events.has(s)) ? i : -1)).filter((i) => i >= 0);
  const last = seenIdx.length ? Math.max(...seenIdx) : -1;

  return PIPELINE.map((step, i) => {
    const evs = step.stages.map((s) => events.get(s)).filter(Boolean);
    const main = evs[evs.length - 1];
    let status;
    if (!evs.length) status = finished || i < last ? "skipped" : "pending";
    else if (finished || i < last) status = "done";
    else if (failed) status = "error";
    else status = main.total && main.done >= main.total && step.key !== "map" ? "done" : "active";
    if (!job && i === 0) status = failed ? "error" : "active";
    return { step, status, event: main };
  });
}

function renderTimeline(job, failed = false) {
  const items = stepStates(job, failed).map(({ step, status, event }) => {
    const node = el("span", { class: "tl-node" });
    if (status === "done") node.append(svg(ICON_CHECK));
    if (status === "error") node.append(svg(ICON_X));

    const count = event && event.total > 1 && status !== "done" ? `${fmt(event.done)}/${fmt(event.total)}` : "";
    let detail = "";
    if (status === "skipped") detail = step.model && state.mode === "facts-only" ? "Skipped: no model configured" : "Not needed";
    else if (status === "pending" && step.model && state.mode === "facts-only") detail = "Needs a model";
    else if (step.key === "parse" && event?.total) detail = `${fmt(event.total)} readable files`;
    else if (step.key === "plan" && state.mode === "facts-only") detail = status === "pending" ? "" : "Used only when a model reads files";
    else if (step.key === "fetch" && event?.message && /^[0-9a-f]{12}$/.test(event.message)) detail = `Commit ${event.message.slice(0, 7)}`;
    else if (event?.message && status !== "pending") detail = event.message;

    const body = el("div", { class: "tl-body" },
      el("div", { class: "tl-label" }, el("span", { text: step.label }), count ? el("span", { class: "tl-count", text: count }) : null),
      detail ? el("div", { class: "tl-detail", text: detail, title: detail }) : null,
    );
    if (status === "active" && event && event.total > 1) {
      const pct = Math.round((100 * event.done) / event.total);
      const bar = el("span");
      bar.style.width = `${pct}%`;
      body.append(el("div", { class: "tl-bar" }, bar));
    }
    return el("li", { class: `tl-step ${status}` }, node, body);
  });
  $("timeline").replaceChildren(...items);
}

function captionFor(job) {
  const active = stepStates(job).find((s) => s.status === "active");
  if (job.status === "queued" || !active) return "Waiting for a worker…";
  const e = active.event;
  const count = e && e.total > 1 ? ` (${fmt(e.done)} of ${fmt(e.total)} ${active.step.unit || ""})` : "";
  return `${active.step.label}${count}…`;
}

/* ---------------------------------------------------------------- results */

async function showResult(job) {
  const r = job.result.report;
  state.markdown = job.result.markdown;
  state.report = r;
  const factsOnly = r.mode === "facts-only";

  setStatus("done", "Ready");
  for (const id of ["share", "copy", "download"]) $(id).disabled = false;
  $("facts-notice").hidden = !factsOnly;

  const meta = [];
  if (r.commit) meta.push(el("span", {}, "Commit ", el("code", { text: r.commit.slice(0, 7) })));
  meta.push(el("span", { text: `${fmt(r.files_read)} files` }));
  const langs = Object.entries(r.languages_loc || {}).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([l]) => l);
  if (langs.length) meta.push(el("span", { text: langs.join(", ") }));
  meta.push(el("span", { text: factsOnly ? "Facts-only" : r.usage?.model || "Claude" }));
  $("repo-meta").replaceChildren(...meta);

  renderStats(r);
  renderReport(r);
  $("raw").textContent = state.markdown;

  const preview = $("preview");
  preview.innerHTML = DOMPurify.sanitize(marked.parse(state.markdown));
  for (const code of preview.querySelectorAll("pre > code.language-mermaid")) {
    const div = el("div", { class: "mermaid" });
    div.dataset.source = code.textContent;
    div.textContent = code.textContent;
    code.parentElement.replaceWith(div);
  }
  for (const pre of preview.querySelectorAll("pre")) {
    pre.append(el("button", { class: "copy-code", type: "button", text: "Copy", onclick: () => copyText(pre.querySelector("code")?.textContent || "", "Code copied") }));
  }
  for (const a of preview.querySelectorAll("a[href^='http']")) { a.target = "_blank"; a.rel = "noopener"; }

  renderDiagramTab();
  $("skeleton").hidden = true;
  $("error-state").hidden = true;
  preview.hidden = false;
  await renderMermaid(preview.querySelectorAll(".mermaid"));
}

function renderStats(r) {
  const factsOnly = r.mode === "facts-only";
  const mv = r.map_verification;
  const unverified = r.unverified_mentions.length;
  const stats = factsOnly
    ? [
        ["Files parsed", fmt(r.files_read)],
        ["With symbols", fmt(r.files_with_symbols)],
        ["Import edges", fmt(r.internal_import_edges)],
        ["Unverified", fmt(unverified), unverified ? "warn" : "ok"],
      ]
    : [
        ["Files read", fmt(r.files_sent_to_model)],
        ["Import edges", fmt(r.internal_import_edges)],
        ["Facts kept", fmt(mv.facts_kept), "ok"],
        ["Claims dropped", fmt(mv.facts_dropped + mv.symbols_dropped)],
        ["Unverified", fmt(unverified), unverified ? "warn" : "ok"],
        ["Cost", r.usage?.estimated_cost_usd != null ? `$${r.usage.estimated_cost_usd.toFixed(2)}` : "–"],
      ];
  $("stats").replaceChildren(...stats.map(([k, v, tone]) => el("div", { class: `stat ${tone || ""}` }, el("dt", { text: k }), el("dd", { text: v }))));
  $("stats-card").hidden = false;

  const count = $("issue-count");
  count.textContent = unverified ? String(unverified) : "✓";
  count.className = `tab-count ${unverified ? "" : "ok"}`;
  count.hidden = false;
}

function renderDiagramTab() {
  const box = $("diagram");
  const match = state.markdown.match(/```mermaid\n([\s\S]*?)```/);
  if (!match) {
    box.replaceChildren(el("p", { class: "empty", text: "No components were found for a diagram." }));
    return;
  }
  const frame = el("div", { class: "diagram-frame" });
  const node = el("div", { class: "mermaid" });
  node.dataset.source = match[1];
  node.textContent = match[1];
  frame.append(node);

  const edges = state.report.diagram_edges || [];
  const parts = [frame, el("p", { class: "diagram-caption", text: edges.length
    ? "Arrows point from a component to the components it imports. Every arrow comes from resolved import statements, never from the model."
    : "No import edges were resolved between these components." })];
  if (edges.length) {
    parts.push(el("div", { class: "table-wrap" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, el("th", { text: "From" }), el("th", { text: "To" }), el("th", { text: "Import edges" }))),
      el("tbody", {}, edges.map((e) => el("tr", {}, el("td", {}, el("code", { text: e.from })), el("td", {}, el("code", { text: e.to })), el("td", { class: "num", text: fmt(e.import_edges) })))),
    )));
  }
  box.replaceChildren(...parts);
  if (!$("tab-diagram").hidden) renderMermaid(box.querySelectorAll(".mermaid"));
}

function renderReport(r) {
  const mv = r.map_verification;
  const factsOnly = r.mode === "facts-only";
  const section = (title, ...children) => el("section", { class: "report-section" }, el("h3", { text: title }), ...children);
  const check = (k, v, sub) => el("div", { class: "check" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v }), sub ? el("div", { class: "sub", text: sub }) : null);
  const issues = (list, render) => el("ul", { class: "issue-list" }, list.map(render));

  const out = [];
  out.push(section("How this README was built",
    el("p", { text: factsOnly
      ? `No model was used. ${fmt(r.files_read)} files were parsed and ${fmt(r.internal_import_edges)} imports were resolved to repository files. Every section comes from the parser, manifests and import graph.`
      : `The model read ${fmt(r.files_sent_to_model)} files in ${fmt(r.map_chunks)} chunks; ${fmt(r.files_skeleton_only)} lower-priority files were represented only by their parsed symbols. Reduce: ${r.reduce_mode}. Repair rounds: ${r.repair_rounds}.` }),
    el("div", { class: "check-grid" },
      check("Files discovered", fmt(r.files_discovered), `${fmt(r.files_read)} readable`),
      check("Import edges", fmt(r.internal_import_edges), "resolved between files"),
      factsOnly ? check("Parse errors", fmt(r.parse_errors)) : check("Facts kept", fmt(mv.facts_kept), `${fmt(mv.facts_dropped)} dropped`),
      factsOnly ? check("Model cost", "$0.00") : check("Symbols kept", fmt(mv.symbols_kept), `${fmt(mv.symbols_dropped)} dropped`),
    ),
  ));

  out.push(section("Unverified mentions",
    r.unverified_mentions.length
      ? issues(r.unverified_mentions, (i) => el("li", {}, el("span", { class: "field", text: i.field }), el("code", { text: i.item }), el("span", { class: "reason", text: i.reason })))
      : el("div", { class: "all-good" }, svg(ICON_CHECK), "Every checked path, symbol and command was found in the repository."),
  ));

  if (r.dropped_from_draft?.length) {
    out.push(section("Removed from the draft",
      el("p", { text: "These entries pointed at things that don't exist, so they were removed before rendering." }),
      issues(r.dropped_from_draft, (i) => el("li", {}, el("span", { class: "field", text: i.field }), el("code", { text: i.item }), el("span", { class: "reason", text: i.reason }))),
    ));
  }

  if (mv.dropped_examples?.length) {
    out.push(section("Claims discarded in the map pass",
      el("p", { text: "Per-file claims whose citations didn't match the source. They never reached the README." }),
      issues(mv.dropped_examples, (d) => el("li", {}, el("span", { class: "field", text: "map" }), el("span", { text: d }))),
    ));
  }

  if (r.map_failures?.length) {
    out.push(section("Failed chunks",
      issues(r.map_failures, (f) => el("li", {}, el("span", { class: "field", text: `chunk ${f.chunk}` }), el("span", { text: `${f.paths.length} files` }), el("span", { class: "reason", text: f.error }))),
    ));
  }

  if (!factsOnly && r.skeleton_only_sample?.length) {
    out.push(section("Files represented by symbols only",
      el("details", { class: "fold" }, el("summary", { text: `${fmt(r.files_skeleton_only)} files over the reading budget` }),
        el("ul", {}, r.skeleton_only_sample.map((p) => el("li", { text: p })))),
    ));
  }

  const stages = Object.entries(r.usage?.stages || {});
  if (stages.length) {
    out.push(section("Model usage",
      el("div", { class: "table-wrap" }, el("table", { class: "data-table" },
        el("thead", {}, el("tr", {}, ["Stage", "Calls", "Input", "Output", "Cache read"].map((h) => el("th", { text: h })))),
        el("tbody", {}, stages.map(([name, s]) => el("tr", {},
          el("td", {}, el("code", { text: name })), el("td", { class: "num", text: fmt(s.calls) }),
          el("td", { class: "num", text: fmt(s.input_tokens) }), el("td", { class: "num", text: fmt(s.output_tokens) }),
          el("td", { class: "num", text: fmt(s.cache_read_tokens) })))),
      )),
    ));
  }

  $("report").replaceChildren(...out);
}

/* ---------------------------------------------------------------- mermaid */

let mermaidTheme = null;
function ensureMermaid() {
  const theme = currentTheme() === "dark" ? "dark" : "neutral";
  if (theme !== mermaidTheme) {
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme, fontFamily: "Inter, system-ui, sans-serif" });
    mermaidTheme = theme;
  }
}

async function renderMermaid(nodes) {
  if (!nodes.length || typeof mermaid === "undefined") return;
  ensureMermaid();
  for (const n of nodes) {
    n.removeAttribute("data-processed");
    n.textContent = n.dataset.source;
  }
  try {
    await mermaid.run({ nodes: [...nodes] });
  } catch (err) {
    console.warn("mermaid render failed", err);
  }
}

function rerenderDiagrams() {
  const visible = [...document.querySelectorAll(".mermaid")].filter((n) => n.offsetParent !== null);
  const hidden = [...document.querySelectorAll(".mermaid")].filter((n) => n.offsetParent === null);
  hidden.forEach((n) => { n.removeAttribute("data-processed"); n.textContent = n.dataset.source; });
  renderMermaid(visible);
}

/* ---------------------------------------------------------------- tabs + actions */

function selectTab(name) {
  for (const t of document.querySelectorAll(".tab")) t.setAttribute("aria-selected", String(t.dataset.tab === name));
  for (const key of ["preview", "markdown", "diagram", "report"]) $(`tab-${key}`).hidden = key !== name;
  // Diagrams can't be measured while hidden, so render (or re-render after a theme change) on show.
  const pending = [...$(`tab-${name}`).querySelectorAll(".mermaid")].filter((n) => !n.querySelector("svg"));
  renderMermaid(pending);
}
for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => selectTab(t.dataset.tab));
$("tabs").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
  const tabs = [...document.querySelectorAll(".tab")];
  const i = tabs.findIndex((t) => t.getAttribute("aria-selected") === "true");
  const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  selectTab(next.dataset.tab);
});

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch {
    toast("Copy failed: your browser blocked clipboard access");
  }
}

$("copy").addEventListener("click", () => copyText(state.markdown, "Markdown copied"));
$("share").addEventListener("click", () => copyText(`${location.origin}/?repo=${encodeURIComponent(state.repo)}`, "Link copied"));
$("download").addEventListener("click", () => {
  const a = el("a", { href: URL.createObjectURL(new Blob([state.markdown], { type: "text/markdown" })), download: "README.md" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast("README.md downloaded");
});

/* ---------------------------------------------------------------- boot */

marked.setOptions({ gfm: true });
renderRecent();
const initial = new URLSearchParams(location.search).get("repo");
if (initial && parseRepo(initial)) {
  $("url").value = `github.com/${parseRepo(initial)}`;
  start(parseRepo(initial), { push: false });
}
