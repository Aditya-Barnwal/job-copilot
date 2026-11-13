const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const enc = encodeURIComponent;

// Public demo (JOBCOPILOT_DEMO=1): the server injects .demo-banner into the
// page; /api/llm/health reporting provider "demo" also switches this on.
let DEMO = !!document.querySelector(".demo-banner");
const DEMO_TAILOR_TIP = "Not available in the public demo — run Job-Copilot locally to tailor";
const DEMO_LINK_TIP = "Sample job — the link is not real in the demo";

// --- tabs ---
const TAB_LOADERS = {
  today: () => loadToday(),
  matches: () => loadMatches(),
  excluded: () => loadExcluded(),
  tracker: () => renderBoard(),
  resume: () => { loadVersions(); loadUserYoe(); loadStyle(); loadProfile(); },
  answers: () => { loadReview(); loadAnswers(); },
  browse: () => loadJobs(),
};

function activeTab() {
  const b = $(".tab.active");
  return b ? b.dataset.tab : "today";
}

function showTab(name) {
  if (!TAB_LOADERS[name]) name = "today";
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  try { history.replaceState(null, "", "#" + name); } catch { /* file:// or sandboxed */ }
  TAB_LOADERS[name]();
}

$$(".tab").forEach((btn) => btn.addEventListener("click", () => showTab(btn.dataset.tab)));

function refreshActive() {
  TAB_LOADERS[activeTab()]();
  loadMeta();
}

// --- helpers ---

// Turns any failure into one plain sentence: the server's `detail` text, a
// demo-mode explanation, or a network hint. Never raw JSON.
function friendlyError(status, payload, path) {
  if (status === 0) return "Couldn't reach the app — is it running?";
  if (status === 403 && payload && payload.demo) {
    return /tailor|cover-letter|email|answers\/preview|resume-options|apply-pack/.test(path || "")
      ? "Not available in the public demo — run Job-Copilot locally to tailor"
      : "Not available in the public demo — run Job-Copilot locally to use this";
  }
  const d = payload && payload.detail;
  if (typeof d === "string" && d.trim()) return d.replace(/^demo: disabled — /, "");
  if (d && typeof d === "object" && !Array.isArray(d) && typeof d.error === "string") return d.error;
  if (Array.isArray(d) && d.length) {
    return "Invalid request: " + d.map((x) => (x && x.msg) || "bad value").join("; ");
  }
  if (status === 404) return "Not found — it may have been removed. Try refreshing.";
  if (status >= 500) return "Something went wrong in the app (server error " + status + "). Check the terminal running it.";
  return "Request failed (" + status + ").";
}

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  } catch (_) {
    const err = new Error(friendlyError(0, null, path));
    err.status = 0; err.payload = null;
    throw err;
  }
  if (!res.ok) {
    const t = await res.text();
    let payload = null;
    try { payload = JSON.parse(t); } catch { payload = null; }
    const err = new Error(friendlyError(res.status, payload, path));
    // Callers that need to react to a specific failure (e.g. the 409 from the
    // server-side grounding gate) read these; everyone else just uses .message.
    err.status = res.status;
    err.payload = payload;
    throw err;
  }
  return res.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(parseFloat(ts) * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(parseFloat(ts) * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

// Safe in text AND in quoted attribute values: quotes are escaped too, so
// model output or scraped job data can't close an attribute and add handlers.
function escapeHtml(s) {
  return (s ?? "").toString()
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Only http(s) URLs may become links; relative paths resolve to this origin.
// Any other scheme (script, data, vbscript, …) or unparsable input -> null.
function safeHttpUrl(u) {
  if (typeof u !== "string" || !u.trim()) return null;
  let url;
  try { url = new URL(u.trim(), window.location.href); } catch { return null; }
  return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
}

// The one place app.js emits href=. A URL that isn't http(s) is shown as
// plain text instead of a link. `attrs` must be a constant string.
function linkHtml(url, label, attrs = "") {
  const href = safeHttpUrl(url);
  if (!href) {
    const shown = url ? ` (${escapeHtml(url)})` : "";
    return `<span class="hint" title="not an http(s) link">${escapeHtml(label)}${shown}</span>`;
  }
  return `<a href="${escapeHtml(href)}" ${attrs}>${escapeHtml(label)}</a>`;
}

function emptyHtml(msg) { return `<div class="empty">${escapeHtml(msg)}</div>`; }
function errorHtml(e) { return `<div class="error-box">${escapeHtml(e.message || String(e))}</div>`; }

// --- toast notifications (non-blocking) ---
let _toastTimer = null;
let _toastUndo = null;
function notify(msg, { undo = null, kind = "info", ms = 6000 } = {}) {
  const t = $("#toast");
  $("#toast-msg").textContent = msg;
  t.classList.remove("toast-error", "toast-info");
  t.classList.add(kind === "error" ? "toast-error" : "toast-info");
  t.hidden = false;
  _toastUndo = undo;
  $("#toast-undo").hidden = !undo;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(hideToast, undo ? Math.max(ms, 9000) : ms);
}
function hideToast() { $("#toast").hidden = true; _toastUndo = null; }
$("#toast-close").addEventListener("click", hideToast);
$("#toast-undo").addEventListener("click", async () => {
  const fn = _toastUndo;
  hideToast();
  if (fn) await fn();
});
function notifyError(e) { notify(e && e.message ? e.message : String(e), { kind: "error", ms: 9000 }); }

// Grounding flags + the confirmation checkbox, shared by every approve flow.
function groundingWarnHtml(g, checkboxId, title = "⚠ Grounding check failed:") {
  const row = (label, items) => (Array.isArray(items) && items.length
    ? `<div>${label}: ${items.map(escapeHtml).join(", ")}</div>` : "");
  return `<div class="grounding-warn">
    <b>${title}</b>
    ${row("inserted keywords not in original", g.inserted_unsupported)}
    ${row("skills not in original", g.unsupported_skills)}
    ${row("new numbers/years not in original", g.new_numbers)}
    ${row("seniority/ownership claims or titles not in original", g.inflated_claims)}
    <label><input type="checkbox" id="${checkboxId}"/> I confirm these are accurate</label>
  </div>`;
}

// --- shared card pieces ---
const STATUS_LABEL = {
  new: "New", found: "Found", interested: "Interested", tailored: "Tailored",
  ready: "Ready to submit", ready_to_submit: "Ready to submit", applied: "Submitted",
  submitted: "Submitted", oa: "OA", interview: "Interview", offer: "Offer",
  rejected: "Rejected", not_interested: "Not interested",
};

function fitClass(s) {
  if (s == null) return "fit-none";
  return s >= 80 ? "fit-hi" : s >= 65 ? "fit-mid" : "fit-lo";
}

function ageText(d) {
  if (d == null) return "posted: unknown";
  if (d === 0) return "posted today";
  return d === 1 ? "posted 1 day ago" : `posted ${d} days ago`;
}

function yoeText(y) {
  if (!y || y.min == null) return null;
  let t = (y.max != null && y.max !== y.min) ? `${y.min}–${y.max} yrs exp` : `${y.min}+ yrs exp`;
  if (y.preferred) t += " (preferred)";
  return t;
}

function viaHtml(j) {
  const name = j.attribution_name || j.source;
  if (!name) return "";
  const label = `via ${name}`;
  return j.attribution_url
    ? linkHtml(j.attribution_url, label, 'class="via" target="_blank" rel="noopener noreferrer"')
    : `<span class="via">${escapeHtml(label)}</span>`;
}

function disabledWithTip(label, tip, cls) {
  return `<span class="tip" title="${escapeHtml(tip)}"><button class="${cls}" disabled aria-disabled="true">${escapeHtml(label)}</button></span>`;
}

function openPostingHtml(j, cls = "btn-secondary") {
  if (DEMO) return disabledWithTip("Open posting", DEMO_LINK_TIP, cls);
  const url = j.apply_url || j.url;
  if (!(typeof url === "string" && url.trim())) return disabledWithTip("Open posting", "No link on this posting", cls);
  return linkHtml(url, "Open posting", `class="${cls}" target="_blank" rel="noopener noreferrer"`);
}

function prepareBtnHtml(key) {
  if (DEMO) return disabledWithTip("Tailor & prepare", DEMO_TAILOR_TIP, "btn-primary");
  return `<button class="btn-primary" data-action="prepare" data-key="${escapeHtml(key)}">Tailor &amp; prepare</button>`;
}

function statusBtn(key, status, label, prev, cls = "btn-secondary") {
  return `<button class="${cls}" data-action="status" data-key="${escapeHtml(key)}"
    data-status="${escapeHtml(status)}" data-prev="${escapeHtml(prev || "")}">${escapeHtml(label)}</button>`;
}

function moreMenu(items) {
  const body = items.filter(Boolean).join("");
  if (!body) return "";
  return `<details class="more"><summary aria-label="More actions">More</summary><div class="menu">${body}</div></details>`;
}

function skillsHtml(matched, missing) {
  const m = (matched || []).map((k) => `<span class="kw have">${escapeHtml(k)}</span>`).join("");
  const x = (missing || []).map((k) => `<span class="kw miss">${escapeHtml(k)}</span>`).join("");
  if (!m && !x) return "";
  return `<div class="skills">
    ${m ? `<span class="skills-label">You have</span>${m}` : ""}
    ${x ? `<span class="skills-label">Missing</span>${x}` : ""}
  </div>`;
}

function fitBreakdownHtml(c) {
  if (!c || typeof c !== "object") return "";
  const rows = Object.entries(c).map(([k, v]) =>
    `<span class="fb-item"><span>${escapeHtml(k)}</span><b>${escapeHtml(Math.round(Number(v)))}</b></span>`).join("");
  return rows ? `<details class="fit-breakdown"><summary>Score breakdown</summary><div class="fb">${rows}</div></details>` : "";
}

// Card for the v2 views (Today, All matches, Hidden roles).
function matchCard(j, { excluded = false } = {}) {
  const el = document.createElement("article");
  el.className = "card" + (excluded ? " card-excluded" : "");
  el.dataset.key = j.key;
  const k = j.key;
  const fit = j.fit_score;
  const tags = [
    DEMO ? `<span class="tag tag-sample" title="${escapeHtml(DEMO_LINK_TIP)}">sample</span>` : "",
    j.tier && j.tier !== "unknown" ? `<span class="tag">${escapeHtml(j.tier)}</span>` : "",
    j.fresher_friendly ? `<span class="tag tag-good">fresher-friendly</span>` : "",
    j.posting_gone ? `<span class="tag tag-bad">no longer listed</span>` : "",
    j.status && j.status !== "new" ? `<span class="tag">${escapeHtml(STATUS_LABEL[j.status] || j.status)}</span>` : "",
  ].join("");
  const facts = [
    j.location ? escapeHtml(j.location) : "",
    j.workplace_type && j.workplace_type !== "unknown" ? escapeHtml(j.workplace_type) : "",
    yoeText(j.yoe) ? escapeHtml(yoeText(j.yoe)) : "",
    `<span class="age ${escapeHtml(j.freshness || "")}">${escapeHtml(ageText(j.age_days))}</span>`,
    viaHtml(j),
  ].filter(Boolean).join('<span class="dot">·</span>');

  let reasons = "";
  if (excluded) {
    const rs = Array.isArray(j.reasons) ? j.reasons : [];
    reasons = `<ul class="reasons">${rs.map((r) => `<li>
        <span class="rule">${escapeHtml(r.rule || "?")}</span>
        <span>${escapeHtml(r.detail || "")}</span>
        ${r.excerpt ? `<q>${escapeHtml(r.excerpt)}</q>` : ""}
      </li>`).join("")}</ul>`;
  }

  const actions = excluded
    ? `<div class="card-actions">${openPostingHtml(j)}</div>`
    : `<div class="card-actions">
        ${prepareBtnHtml(k)}
        ${openPostingHtml(j)}
        ${statusBtn(k, "submitted", "✓ Submitted", j.status)}
        ${moreMenu([
          statusBtn(k, "found", "Interested", j.status, "menu-item"),
          statusBtn(k, "not_interested", "Not interested", j.status, "menu-item"),
        ])}
      </div>`;

  el.innerHTML = `
    <div class="card-top">
      ${excluded ? "" : `<div class="fit ${fitClass(fit)}" title="Fit score (0–100)">${fit == null ? "–" : escapeHtml(Math.round(fit))}</div>`}
      <div class="card-head">
        <div class="card-title">${escapeHtml(j.title || "Untitled role")}</div>
        <div class="card-company">${escapeHtml(j.company || "Unknown company")}</div>
      </div>
      <div class="tags">${tags}</div>
    </div>
    ${j.why && !excluded ? `<p class="why">${escapeHtml(j.why)}</p>` : ""}
    <div class="facts">${facts}</div>
    ${excluded ? reasons : skillsHtml(j.matched_skills, j.missing_skills)}
    ${excluded ? "" : fitBreakdownHtml(j.fit_components)}
    ${actions}`;
  return el;
}

// --- legacy card (All stored) ---
function jobCard(j) {
  const pay = j.comp_max
    ? `~${Math.round((j.comp_max * (j.comp_currency === "USD" ? 83 : 1)) / 100000)} LPA`
    : j.package_estimated ? "pay est." : "";
  const geo = (j.country_bucket || "?") + (j.country_uncertain ? "?" : "");
  const ageBadge = j.freshness
    ? `<span class="badge ${escapeHtml(j.freshness)}">${escapeHtml(j.posted_age_days ?? "?")}d · ${escapeHtml(j.freshness)}</span>`
    : "";
  const scoreBadge = j.match_score != null
    ? `<span class="badge score">match ${j.match_score.toFixed(1)}</span>` : "";
  const atsBadge = j.ats_coverage != null
    ? `<span class="badge ats">ATS ${Math.round(j.ats_coverage * 100)}%</span>` : "";
  const senBadge = j.seniority_high ? `<span class="tag">senior+</span>` : "";
  const exBadge = j.excluded ? `<span class="tag tag-bad">hidden by rules</span>` : "";
  const sampleBadge = DEMO ? `<span class="tag tag-sample" title="${escapeHtml(DEMO_LINK_TIP)}">sample</span>` : "";

  const fitBadge = j.fit_score != null ? `<span class="badge fit">fit ${escapeHtml(j.fit_score)}</span>` : "";
  const lowConfBadge = j.ats_low_confidence ? `<span class="badge lowconf">low signal · ${(j.required_keywords || []).length} kw</span>` : "";
  const yoeBadge = j.min_yoe_required != null
    ? `<span class="badge ${j.yoe_gap ? 'yoe-gap' : (j.yoe_is_preferred ? 'yoe-soft' : 'yoe-req')}">YOE ≥ ${escapeHtml(j.min_yoe_required)}${j.yoe_is_preferred ? ' (pref)' : ''}${j.yoe_gap ? ' · gap' : ''}</span>`
    : "";

  const missing = Array.isArray(j.missing_keywords) ? j.missing_keywords : [];
  const required = Array.isArray(j.required_keywords) ? j.required_keywords : [];
  const missingRow = required.length
    ? `<div class="skills">
         <span class="skills-label">Required (${required.length})</span>
         ${required.map((kw) => {
           const have = !missing.includes(kw.toLowerCase());
           return `<span class="kw ${have ? "have" : "miss"}">${escapeHtml(kw)}</span>`;
         }).join("")}
       </div>` : "";

  const k = j.key;
  const div = document.createElement("article");
  div.className = "card";
  div.dataset.key = j.key;
  div.innerHTML = `
    <div class="card-top">
      <div class="card-head">
        <div class="card-title">${escapeHtml(j.title)}</div>
        <div class="card-company">${escapeHtml(j.company)}</div>
      </div>
      <div class="tags">${sampleBadge}${senBadge}${exBadge}<span class="tag">${escapeHtml(STATUS_LABEL[j.status] || j.status || "new")}</span></div>
    </div>
    <div class="facts">
      ${[escapeHtml(j.role_family || "?"), escapeHtml(j.workplace_type || "?"), escapeHtml(geo), pay ? escapeHtml(pay) : ""]
        .filter(Boolean).join('<span class="dot">·</span>')}
    </div>
    <div class="badges">${ageBadge}${fitBadge}${scoreBadge}${atsBadge}${lowConfBadge}${yoeBadge}</div>
    ${missingRow}
    <div class="card-actions">
      ${prepareBtnHtml(k)}
      ${openPostingHtml(j)}
      ${statusBtn(k, "submitted", "✓ Submitted", j.status)}
      ${moreMenu([
        statusBtn(k, "interested", "Interested", j.status, "menu-item"),
        statusBtn(k, "not_interested", "Not interested", j.status, "menu-item"),
        !DEMO ? `<button class="menu-item" data-action="tailor" data-key="${escapeHtml(k)}">Tailor only</button>` : "",
        j.apply_method === "email" && !DEMO
          ? `<button class="menu-item" data-action="draft-email" data-key="${escapeHtml(k)}">Draft email</button>` : "",
      ])}
    </div>
  `;
  return div;
}

// --- status changes (with undo) ---
async function setStatus(key, status, prev) {
  await api(`/api/jobs/${enc(key)}/status`, {
    method: "POST",
    body: JSON.stringify({ status, source: "user" }),
  });
  const label = STATUS_LABEL[status] || status;
  notify(`Moved to ${label}.`, prev ? {
    undo: async () => {
      try {
        await api(`/api/jobs/${enc(key)}/status`, {
          method: "POST", body: JSON.stringify({ status: prev, source: "user", note: "undo" }),
        });
        notify("Undone.");
        refreshActive();
      } catch (e) { notifyError(e); }
    },
  } : {});
  refreshActive();
}

async function handleStatusClick(target) {
  const key = target.dataset.key;
  const status = target.dataset.status;
  if (!key || !status) return;
  target.disabled = true;
  const menu = target.closest("details.more");
  if (menu) menu.open = false;
  const inModal = !!target.closest("#tailor-modal");
  try {
    await setStatus(key, status, target.dataset.prev || null);
    if (inModal) hideModal();
  } catch (e) {
    notifyError(e);
    target.disabled = false;
  }
}

// Undo from the tracker: go back to the status before the latest change.
async function undoLast(btn) {
  const key = btn.dataset.key;
  btn.disabled = true;
  try {
    const h = await api(`/api/jobs/${enc(key)}/history`);
    const hist = h.history || [];
    const prev = hist.length >= 2 ? hist[hist.length - 2].status : "new";
    await api(`/api/jobs/${enc(key)}/status`, {
      method: "POST", body: JSON.stringify({ status: prev, source: "user", note: "undo" }),
    });
    notify(`Undone — back to ${STATUS_LABEL[prev] || prev}.`);
    refreshActive();
  } catch (e) {
    notifyError(e);
    btn.disabled = false;
  }
}

// Single, document-level delegated click listener (no inline handlers: CSP).
document.addEventListener("click", (ev) => {
  // Close any open "More" menu when clicking elsewhere.
  $$("details.more[open]").forEach((d) => { if (!d.contains(ev.target)) d.open = false; });

  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  try {
    if (action === "status") handleStatusClick(btn);
    else if (action === "undo") undoLast(btn);
    else if (action === "apply") loadJobs();
    else if (action === "prepare") openApplyPicker(btn.dataset.key);
    else if (action === "tailor") openTailorModal(btn.dataset.key);
    else if (action === "draft-email") openEmailModal(btn.dataset.key);
    else if (action === "export-csv") downloadCsv();
    else if (action === "answer-del") deleteAnswer(btn.dataset.q);
    else if (action === "copy") copyText(btn.dataset.text, btn);
    else if (action === "apply-job") openApplyPicker(btn.dataset.key);
    else if (action === "apply-pack") openApplyPack(btn.dataset.key);
    else if (action === "pick-resume") pickResume(btn.dataset.key, btn.dataset.source);
    else if (action === "x-rule") { excludedState.rule = btn.dataset.rule || null; loadExcluded(); }
    else if (action === "review-filter") { reviewFilter = btn.dataset.filter; renderReview(); }
  } catch (e) {
    console.error("[app.js] click handler threw:", e);
    notifyError(e);
  }
});

// Enter inside any filter input also applies.
$$("#tab-browse .filters input").forEach((i) => {
  i.addEventListener("keydown", (e) => { if (e.key === "Enter") loadJobs(); });
});
$$("#m-filters input").forEach((i) => {
  i.addEventListener("keydown", (e) => { if (e.key === "Enter") { matchesState.offset = 0; loadMatches(); } });
});

// --- Today ---
// Queue/matches items are flat job summaries; accept {job: {...}} wrappers too.
function flatItem(it) {
  return it && typeof it === "object" && it.job && typeof it.job === "object" ? { ...it.job, ...it, job: undefined } : it;
}
function itemsOf(d) {
  const list = Array.isArray(d) ? d : (d && (d.items || d.jobs || d[d.lane] || [])) || [];
  return list.map(flatItem).filter((x) => x && x.key);
}

function fillCards(container, items, emptyMsg, opts) {
  container.innerHTML = "";
  const list = (items || []).map(flatItem).filter((x) => x && x.key);
  if (!list.length) { container.innerHTML = emptyHtml(emptyMsg); return; }
  for (const j of list) container.appendChild(matchCard(j, opts));
}

async function loadToday() {
  const main = $("#today-main");
  main.innerHTML = '<div class="hint">Loading…</div>';
  loadRunStatus();
  try {
    const [m, x, n] = await Promise.all([
      api("/api/queue?lane=main"),
      api("/api/queue?lane=extended"),
      api("/api/queue?lane=near_miss"),
    ]);
    if (!m.day) {
      $("#today-summary").textContent = "";
      main.innerHTML = emptyHtml("No list yet. Click “Run daily now” to fetch jobs and build today's shortlist.");
    } else {
      const d = new Date(m.day + "T00:00:00");
      const dayLabel = isNaN(d) ? m.day : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      const mainItems = itemsOf(m);
      $("#today-summary").textContent = `${dayLabel} · ${plural(mainItems.length, "role")} on today's list`;
      fillCards(main, mainItems, "Nothing left on today's list — you've handled them all, or nothing passed your filters. See All matches.");
    }
    const xi = itemsOf(x), ni = itemsOf(n);
    $("#today-extended-count").textContent = String(xi.length);
    fillCards($("#today-extended"), xi, "No watchlist roles today.");
    $("#today-near-count").textContent = String(ni.length);
    fillCards($("#today-near"), ni, "No near misses today.");
  } catch (e) {
    main.innerHTML = errorHtml(e);
  }
}

let _runPoll = null;
async function loadRunStatus() {
  const el = $("#run-status");
  try {
    const r = await api("/api/runs?limit=1");
    const last = (r.runs || [])[0];
    const btn = $("#run-daily");
    if (r.running) {
      el.textContent = "Daily run in progress…";
      btn.disabled = true;
      if (!_runPoll) _runPoll = setInterval(pollRun, 5000);
    } else {
      btn.disabled = DEMO;
      el.textContent = last
        ? `Last run: ${fmtTs(last.finished_at || last.started_at)} · ${last.status}`
        : "No runs yet";
    }
    if (last) {
      $("#last-run").textContent = `last run: ${fmtTs(last.finished_at || last.started_at)}`;
      $("#last-run").dataset.fromRuns = "1";
    }
  } catch (e) {
    el.textContent = "";
  }
}

async function pollRun() {
  try {
    const r = await api("/api/runs?limit=1");
    if (!r.running) {
      clearInterval(_runPoll); _runPoll = null;
      const last = (r.runs || [])[0];
      notify(last && last.status === "ok" ? "Daily run finished." : `Daily run ended: ${last ? last.status : "unknown"}.`,
        { kind: last && last.status === "ok" ? "info" : "error" });
      refreshActive();
      loadRunStatus();
    }
  } catch { /* keep polling */ }
}

$("#run-daily").addEventListener("click", async () => {
  const btn = $("#run-daily");
  btn.disabled = true;
  try {
    await api("/api/runs/daily", { method: "POST", body: "{}" });
    notify("Daily run started. This page refreshes when it finishes.");
    $("#run-status").textContent = "Daily run in progress…";
    if (!_runPoll) _runPoll = setInterval(pollRun, 5000);
  } catch (e) {
    notifyError(e);
    btn.disabled = false;
  }
});

// --- All matches ---
const matchesState = { offset: 0, limit: 25 };

async function loadMatches() {
  const list = $("#matches-list");
  const params = new URLSearchParams();
  const q = $("#m-q").value.trim(); if (q) params.set("q", q);
  const loc = $("#m-location").value.trim(); if (loc) params.set("location", loc);
  const src = $("#m-source").value.trim(); if (src) params.set("source", src);
  const tier = $("#m-tier").value; if (tier) params.set("tier", tier);
  params.set("lane", $("#m-lane").value || "main");
  params.set("sort", $("#m-sort").value || "fit");
  params.set("limit", String(matchesState.limit));
  params.set("offset", String(matchesState.offset));
  list.innerHTML = '<div class="hint">Loading…</div>';
  try {
    const d = await api("/api/matches?" + params.toString());
    fillCards(list, d.items || [], "No roles match these filters. Try clearing some.");
    const from = d.total ? d.offset + 1 : 0;
    const to = d.offset + (d.items || []).length;
    $("#m-page").textContent = `${from}–${to} of ${d.total}`;
    $("#m-prev").disabled = d.offset <= 0;
    $("#m-next").disabled = to >= d.total;
  } catch (e) {
    list.innerHTML = errorHtml(e);
    $("#m-page").textContent = "";
  }
}
$("#m-apply").addEventListener("click", () => { matchesState.offset = 0; loadMatches(); });
["#m-tier", "#m-lane", "#m-sort"].forEach((s) =>
  $(s).addEventListener("change", () => { matchesState.offset = 0; loadMatches(); }));
$("#m-prev").addEventListener("click", () => {
  matchesState.offset = Math.max(0, matchesState.offset - matchesState.limit); loadMatches(); window.scrollTo(0, 0);
});
$("#m-next").addEventListener("click", () => {
  matchesState.offset += matchesState.limit; loadMatches(); window.scrollTo(0, 0);
});

// --- Hidden roles (excluded) ---
const RULE_LABEL = {
  geo: "Location", role: "Role type", seniority: "Seniority", yoe: "Experience required",
  company: "Company", salary: "Salary", tier: "Company tier", stale: "Too old", duplicate: "Duplicate",
};
const excludedState = { rule: null, offset: 0, limit: 100, items: [] };

function ruleLabel(r) { return RULE_LABEL[r] || r; }

async function loadExcluded(append = false) {
  const list = $("#excluded-list");
  if (!append) { excludedState.offset = 0; excludedState.items = []; list.innerHTML = '<div class="hint">Loading…</div>'; }
  const params = new URLSearchParams({ limit: String(excludedState.limit), offset: String(excludedState.offset) });
  if (excludedState.rule) params.set("rule", excludedState.rule);
  try {
    const d = await api("/api/excluded?" + params.toString());
    const allTotal = Object.values(d.by_rule || {}).reduce((a, b) => a + b, 0);
    $("#x-rules").innerHTML =
      `<button class="chip ${excludedState.rule ? "" : "active"}" data-action="x-rule" data-rule="">All</button>` +
      Object.entries(d.by_rule || {}).map(([r, n]) =>
        `<button class="chip ${excludedState.rule === r ? "active" : ""}" data-action="x-rule" data-rule="${escapeHtml(r)}">${escapeHtml(ruleLabel(r))} <span class="count">${n}</span></button>`
      ).join("");
    excludedState.items = excludedState.items.concat(d.items || []);
    excludedState.offset = excludedState.items.length;
    renderExcluded(d.total, allTotal);
  } catch (e) {
    list.innerHTML = errorHtml(e);
  }
}

function renderExcluded(total, allTotal) {
  const list = $("#excluded-list");
  list.innerHTML = "";
  if (!excludedState.items.length) {
    list.innerHTML = emptyHtml(allTotal ? "No hidden roles for this rule." : "Nothing has been hidden by your rules yet.");
  }
  // Group by the selected rule, or by each job's first (main) reason.
  const groups = new Map();
  for (const j of excludedState.items) {
    const rules = (j.reasons || []).map((r) => r.rule || "?");
    const g = excludedState.rule && rules.includes(excludedState.rule) ? excludedState.rule : (rules[0] || "?");
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(j);
  }
  for (const [rule, items] of groups) {
    const sec = document.createElement("section");
    sec.className = "group";
    sec.innerHTML = `<h3 class="group-title">${escapeHtml(ruleLabel(rule))} <span class="count">${items.length}</span></h3>`;
    const cards = document.createElement("div");
    cards.className = "cards";
    for (const j of items) cards.appendChild(matchCard(j, { excluded: true }));
    sec.appendChild(cards);
    list.appendChild(sec);
  }
  $("#x-page").textContent = total ? `Showing ${excludedState.items.length} of ${total}` : "";
  $("#x-more").hidden = excludedState.items.length >= total;
}
$("#x-more").addEventListener("click", () => loadExcluded(true));

// --- Tracker ---
const BOARD_COLS = [
  ["found", "Found"], ["tailored", "Tailored"], ["ready_to_submit", "Ready to submit"],
  ["submitted", "Submitted"], ["oa", "OA"], ["interview", "Interview"],
  ["offer", "Offer"], ["rejected", "Rejected"],
];
const NEXT_STEP = {
  found: ["tailored", "Mark tailored"],
  tailored: ["ready_to_submit", "Ready to submit"],
  ready_to_submit: ["submitted", "✓ Submitted"],
  submitted: ["oa", "Got OA"],
  oa: ["interview", "Interview"],
  interview: ["offer", "Offer"],
};

function trackerCard(j, col) {
  const el = document.createElement("article");
  el.className = "tcard";
  const k = j.key;
  const next = NEXT_STEP[col];
  const preSubmit = ["found", "tailored"].includes(col);
  const active = ["submitted", "oa", "interview"].includes(col);
  const since = j.days_in_status != null
    ? (j.days_in_status === 0 ? "today" : `${plural(j.days_in_status, "day")}`) : null;
  el.innerHTML = `
    <div class="tcard-company">${escapeHtml(j.company || "")}${DEMO ? ' <span class="tag tag-sample">sample</span>' : ""}</div>
    <div class="tcard-title">${escapeHtml(j.title || "")}</div>
    <div class="tcard-meta">${escapeHtml(fmtDate(j.status_changed_at))}${since ? ` · ${escapeHtml(since)} here` : ""}
      ${j.submitted_at && col !== "submitted" ? ` · submitted ${escapeHtml(fmtDate(j.submitted_at))}` : ""}</div>
    ${j.warning ? `<div class="tcard-warn">${escapeHtml(j.warning)}</div>` : ""}
    <div class="tcard-actions">
      ${next ? statusBtn(k, next[0], next[1], j.status, next[0] === "submitted" ? "btn-primary btn-sm" : "btn-secondary btn-sm") : ""}
      ${preSubmit ? statusBtn(k, "submitted", "✓ Submitted", j.status, "btn-secondary btn-sm") : ""}
      <button class="ghost btn-sm" data-action="undo" data-key="${escapeHtml(k)}" title="Go back to the previous status">Undo</button>
      ${moreMenu([
        !DEMO && (j.apply_url || j.url) ? linkHtml(j.apply_url || j.url, "Open posting", 'class="menu-item" target="_blank" rel="noopener noreferrer"') : "",
        active || preSubmit || col === "ready_to_submit" ? statusBtn(k, "rejected", "Rejected", j.status, "menu-item") : "",
        col !== "rejected" && col !== "offer" ? statusBtn(k, "not_interested", "Not interested", j.status, "menu-item") : "",
      ])}
    </div>`;
  return el;
}

async function renderBoard() {
  const board = $("#board");
  board.innerHTML = '<div class="hint">Loading…</div>';
  let t;
  try {
    t = await api("/api/tracker");
  } catch (e) {
    board.innerHTML = errorHtml(e);
    return;
  }
  renderReminders(t);
  const byStatus = Object.fromEntries((t.columns || []).map((c) => [c.status, c]));
  board.innerHTML = "";
  let total = 0;
  for (const [status, label] of BOARD_COLS) {
    const c = byStatus[status] || { count: 0, items: [] };
    total += c.count;
    const col = document.createElement("div");
    col.className = "column col-" + status;
    col.innerHTML = `<h3>${escapeHtml(label)} <span class="count">${c.count}</span></h3>`;
    const body = document.createElement("div");
    body.className = "column-body";
    if (!c.items.length) body.innerHTML = '<div class="col-empty">—</div>';
    for (const j of c.items) body.appendChild(trackerCard(j, status));
    col.appendChild(body);
    board.appendChild(col);
  }
  const ni = byStatus.not_interested ? byStatus.not_interested.count : 0;
  $("#board-foot").textContent = (total ? "" : "Nothing tracked yet. Use “✓ Submitted” or “More → Interested” on a job card to add it here. ")
    + (ni ? `${plural(ni, "job")} marked not interested (hidden).` : "");
}

function renderReminders(t) {
  const box = $("#reminders");
  const ready = (t.ready_to_submit || []).filter((j) => j.reminder_due || j.warning);
  const fu = t.followups_due || [];
  if (!ready.length && !fu.length) { box.innerHTML = ""; return; }
  const row = (j, text, actions) => `<li><span class="rem-text"><b>${escapeHtml(j.company || "")}</b> — ${escapeHtml(j.title || "")}
      <span class="hint">${escapeHtml(text)}</span></span><span class="rem-actions">${actions}</span></li>`;
  box.innerHTML = `<div class="reminder-box">
    <h3>Reminders</h3>
    <ul>
      ${ready.map((j) => row(j,
        j.warning ? `ready to submit, but the ${j.warning}` : `ready to submit for ${plural(j.days_waiting || 0, "day")} — submit it or drop it`,
        statusBtn(j.key, "submitted", "✓ Submitted", j.status, "btn-secondary btn-sm"))).join("")}
      ${fu.map((j) => row(j,
        `submitted ${plural(j.days_since_submit || 0, "day")} ago — time for a polite follow-up`,
        statusBtn(j.key, "rejected", "Rejected", j.status, "ghost btn-sm"))).join("")}
    </ul></div>`;
}

// --- legacy browse ---
function legacyParams() {
  const params = new URLSearchParams();
  const q = $("#f-q").value.trim(); if (q) params.set("q", q);
  const sec = $("#f-sector").value; if (sec) params.set("sector", sec);
  const geo = $("#f-geo").value; if (geo) params.set("country_bucket", geo);
  const work = $("#f-work").value; if (work) params.set("workplace_type", work);
  const st = $("#f-status").value; if (st) params.set("status", st);
  const rf = $("#f-role").value.trim(); if (rf) params.set("role_family", rf);
  const fr = $("#f-fresh").value; if (fr) params.set("freshness", fr);
  const ms = $("#f-min-score").value; if (ms) params.set("min_score", ms);
  const my = $("#f-max-yoe").value; if (my) params.set("max_yoe", my);
  if ($("#f-hide-stale").checked) params.set("max_age_days", "180");
  if ($("#f-hide-senior").checked) params.set("hide_senior_high", "true");
  return params;
}

async function loadJobs() {
  const params = legacyParams();
  if ($("#f-include-excluded").checked) params.set("include_excluded", "true");
  const list = $("#jobs-list");
  list.innerHTML = '<div class="hint">Loading…</div>';
  try {
    const data = await api("/api/jobs?" + params.toString());
    list.innerHTML = "";
    if (!data.jobs.length) {
      list.innerHTML = emptyHtml("No jobs match. Run ingest, or relax the filters.");
      return;
    }
    for (const j of data.jobs) list.appendChild(jobCard(j));
  } catch (e) {
    list.innerHTML = errorHtml(e);
  }
}

function downloadCsv() {
  window.open("/api/export.csv?" + legacyParams().toString(), "_blank");
}

$("#run-score").addEventListener("click", async () => {
  const btn = $("#run-score");
  btn.disabled = true; btn.textContent = "Scoring…";
  try {
    const res = await api("/api/score", { method: "POST", body: "{}" });
    notify(`Scored ${res.scored} jobs (résumé keywords: ${res.resume_keyword_count}).`);
    await loadJobs();
  } catch (e) {
    notifyError(e);
  } finally {
    btn.disabled = false; btn.textContent = "Score all";
  }
});

$("#run-ingest").addEventListener("click", async () => {
  const btn = $("#run-ingest");
  btn.disabled = true; btn.textContent = "Running…";
  try {
    const res = await api("/api/ingest", { method: "POST", body: "{}" });
    notify(`Ingest done: ${res.ingested_raw} raw → ${res.passed} kept (${res.inserted} new, ${res.updated} updated).`);
    await loadMeta();
    await loadJobs();
  } catch (e) {
    notifyError(e);
  } finally {
    btn.disabled = false; btn.textContent = "Run ingest";
  }
});

// --- resume ---
$("#resume-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  let res;
  try {
    res = await fetch("/api/resume", { method: "POST", body: fd });
  } catch {
    notifyError(new Error(friendlyError(0)));
    return;
  }
  if (!res.ok) {
    let payload = null;
    try { payload = await res.json(); } catch { /* not JSON */ }
    notifyError(new Error(friendlyError(res.status, payload, "/api/resume")));
    return;
  }
  notify("Résumé uploaded.");
  await loadMeta();
});

async function loadVersions() {
  const div = $("#versions-list");
  try {
    const data = await api("/api/resume-versions");
    if (!data.versions.length) {
      div.textContent = "None yet. Tailor a résumé for a job (Today → Tailor & prepare) and approve it to save a version here.";
      return;
    }
    div.classList.remove("hint");
    div.innerHTML = data.versions.map((v) =>
      `<div class="ap-row">
        <div class="ap-q">${escapeHtml(v.label)}</div>
        <div class="ap-a">${escapeHtml(v.role_family || "")} · ATS ${Math.round((v.base_ats_coverage || 0) * 100)}%</div>
        <div>${v.pdf_path ? linkHtml(`/api/resume-versions/${enc(v.id)}/download`, "PDF", 'class="btn-secondary" target="_blank" rel="noopener"') : ""}</div>
      </div>`
    ).join("");
  } catch (e) {
    div.innerHTML = errorHtml(e);
  }
}

// --- meta header ---
async function loadMeta() {
  try {
    const m = await api("/api/meta");
    $("#last-ingest").textContent = "last ingest: " + fmtTs(m.last_ingest);
    // Header falls back to the last ingest until /api/runs answers.
    if (!$("#last-run").dataset.fromRuns && m.last_ingest) $("#last-run").textContent = "last run: " + fmtTs(m.last_ingest);
    $("#resume-status").textContent = m.resume_loaded
      ? `résumé: ${m.resume_filename || "loaded"}`
      : "résumé: not uploaded";
    $("#resume-info").textContent = m.resume_loaded ? `Loaded: ${m.resume_filename}` : "No résumé uploaded.";
  } catch { /* header is best-effort */ }
}

// --- LLM badge ---
async function loadLlmStatus() {
  const el = $("#llm-status");
  if (!el) return;
  try {
    const h = await api("/api/llm/health");
    if (h.provider === "demo") {
      el.className = "badge llm-unknown";
      el.textContent = "LLM: off in demo";
      el.title = "The public demo has no LLM; tailoring is disabled.";
      if (!DEMO) { DEMO = true; applyDemoLocks(); TAB_LOADERS[activeTab()](); }
      return;
    }
    const ok = h.reachable && h.model_ok;
    el.className = "badge " + (ok ? "llm-ok" : "llm-bad");
    el.textContent = `LLM: ${h.provider}/${h.model || h.configured || "?"} ${ok ? "✓" : "✗"}`;
    el.title = h.hint || "";
  } catch (e) {
    el.className = "badge llm-bad";
    el.textContent = "LLM: ?";
    el.title = e.message;
  }
}

// --- tailor modal ---
let tailorState = { key: null, suggestion: null, tailor: null };

function showModal() { $("#tailor-modal").classList.remove("hidden"); }
function hideModal() {
  $("#tailor-modal").classList.add("hidden");
  tailorState = { key: null, suggestion: null, tailor: null };
  $("#modal-approve").disabled = true;
  $("#modal-status").textContent = "";
}

$("#modal-close").addEventListener("click", hideModal);
$("#modal-cancel").addEventListener("click", hideModal);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#tailor-modal").classList.contains("hidden")) hideModal();
});

async function openTailorModal(key) {
  tailorState.key = key;
  $("#modal-title").textContent = "Tailor";
  $("#modal-body").innerHTML = '<div class="hint">Checking for reusable versions…</div>';
  $("#modal-approve").disabled = true;
  $("#modal-status").textContent = "";
  showModal();

  let suggestion;
  try {
    suggestion = await api(`/api/jobs/${enc(key)}/tailor-suggestion`);
  } catch (e) {
    $("#modal-body").innerHTML = errorHtml(e);
    return;
  }
  tailorState.suggestion = suggestion;

  if (suggestion.mode === "reuse") {
    const v = suggestion.version;
    $("#modal-body").innerHTML = `
      <p>A stored version already covers
         <b>${Math.round(suggestion.coverage * 100)}%</b> of this job's keywords.</p>
      <div class="section-card">
        <b>${escapeHtml(v.label || "version " + v.id)}</b><br/>
        role family: ${escapeHtml(v.role_family || "—")}<br/>
        coverage for this job: ${Math.round(v.coverage * 100)}%<br/>
        base ATS coverage: ${Math.round((v.base_ats_coverage || 0) * 100)}%
      </div>
      <p class="hint">Reuse skips the LLM call entirely — no new file is generated.</p>
      <div id="reuse-grounding"></div>
    `;
    $("#modal-approve").textContent = "Use this version";
    $("#modal-approve").disabled = false;
    let reuseGrounding = null;   // set when the server flags the stored text
    $("#modal-approve").onclick = async () => {
      const body = { reuse_version_id: v.id };
      if (reuseGrounding) body.confirmed_flags = confirmedFlagsFrom(reuseGrounding);
      try {
        const res = await api(`/api/jobs/${enc(key)}/approve`, {
          method: "POST", body: JSON.stringify(body),
        });
        notify(`Linked to version ${res.version_id}. Status: ready to submit.`);
        hideModal(); refreshActive();
      } catch (e) {
        if (e.status === 409 && e.payload) {
          // The server re-checked the stored text against your CURRENT resume.
          // Nothing was linked; show what it found and require confirmation.
          $("#modal-status").textContent =
            (typeof e.payload.detail === "string" && e.payload.detail) || "Grounding check failed on the server. Nothing was linked.";
          if (!e.payload.grounding) { $("#modal-approve").disabled = true; return; }
          reuseGrounding = e.payload.grounding;
          $("#reuse-grounding").innerHTML = groundingWarnHtml(reuseGrounding, "confirm-grounding-reuse");
          $("#modal-approve").disabled = true;
          $("#confirm-grounding-reuse").addEventListener("change", (ev) => {
            $("#modal-approve").disabled = !ev.target.checked;
          });
          return;
        }
        notifyError(e);
      }
    };
    return;
  }

  // Fresh path: call /tailor (this is the only LLM call in the flow)
  $("#modal-body").innerHTML = '<div class="hint">Tailoring (this may take 10–60s)…</div>';
  let t;
  try {
    t = await api(`/api/jobs/${enc(key)}/tailor`, {
      method: "POST", body: JSON.stringify({}),
    });
  } catch (e) {
    const llmBadge = $("#llm-status");
    const llmOk = llmBadge && llmBadge.classList.contains("llm-ok");
    const hint = e.status === 403 || e.status === 0 ? "" : llmOk
      ? "The LLM is reachable (badge is green). This is usually a malformed-JSON reply from the small local model. Consider LLM_PROVIDER=groq (free tier, stronger model) — see README."
      : "The LLM badge is not green. Start Ollama or set LLM_PROVIDER in .env.";
    $("#modal-body").innerHTML = `${errorHtml(e)}${hint ? `<p class="hint">${hint}</p>` : ""}`;
    return;
  }
  tailorState.tailor = t;
  renderTailorResult(t);
}

function renderTailorResult(t) {
  const before = Math.round((t.before_ats_coverage || 0) * 100);
  const after = Math.round((t.new_ats_coverage || 0) * 100);
  const stillMissing = (t.still_missing || []).map((k) => `<span class="kw miss">${escapeHtml(k)}</span>`).join(" ");
  const grounding = t.grounding || {};
  const flags = !grounding.clean ? groundingWarnHtml(grounding, "confirm-grounding") : "";

  $("#modal-body").innerHTML = `
    <div class="ats-row">
      <span class="badge ats">ATS before: ${before}%</span>
      <span class="badge ats">ATS after: ${after}%</span>
    </div>
    ${flags}
    <h4>Diff</h4>
    <pre class="diff">${escapeHtml(t.diff || "(no diff)")}</pre>
    <h4>Still missing</h4>
    <div>${stillMissing || '<span class="hint">none</span>'}</div>
  `;

  $("#modal-approve").textContent = "Approve & save PDF";
  const canApprove = grounding.clean;
  $("#modal-approve").disabled = !canApprove;

  if (!canApprove) {
    const cb = $("#confirm-grounding");
    if (cb) cb.addEventListener("change", () => {
      $("#modal-approve").disabled = !cb.checked;
    });
  }

  $("#modal-approve").onclick = async () => {
    const body = { tailored_text: t.tailored_text };
    // The server re-runs grounding on this exact text and refuses (409)
    // unless every flagged item is explicitly confirmed. Send exactly the
    // lists the user just confirmed in this modal.
    if (!grounding.clean) body.confirmed_flags = confirmedFlagsFrom(grounding);
    try {
      const res = await api(`/api/jobs/${enc(tailorState.key)}/approve`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      notify(`Saved version ${res.version_id} (ATS ${Math.round(res.new_ats_coverage * 100)}%). Status: ready to submit.`);
      hideModal();
      refreshActive();
    } catch (e) {
      if (e.status === 409 && e.payload && e.payload.grounding) {
        // The server found flags not confirmed here (e.g. the resume or job
        // keywords changed since tailoring). Show its findings and require a
        // fresh confirmation; nothing was saved.
        t.grounding = e.payload.grounding;
        renderTailorResult(t);
        $("#modal-status").textContent =
          (typeof e.payload.detail === "string" && e.payload.detail) || "Grounding check failed on the server. Nothing was saved.";
        return;
      }
      notifyError(e);
    }
  };
}

const GROUNDING_FLAG_KEYS = ["inserted_unsupported", "unsupported_skills", "new_numbers", "inflated_claims"];

function confirmedFlagsFrom(grounding) {
  const out = {};
  for (const k of GROUNDING_FLAG_KEYS) out[k] = Array.isArray(grounding[k]) ? [...grounding[k]] : [];
  return out;
}

// --- profile ---
const _profileFields = ["first_name", "last_name", "email", "phone", "linkedin_url", "portfolio_url"];

async function loadProfile() {
  try {
    const p = await api("/api/profile");
    for (const f of _profileFields) {
      const el = document.getElementById(`p-${f.replace(/_/g, "-")}`);
      if (el) el.value = p[f] || "";
    }
    const filled = _profileFields.filter((f) => (p[f] || "").trim()).length;
    $("#profile-status").textContent = `${filled}/${_profileFields.length} fields set`;
  } catch { /* optional */ }
}

$("#profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const f of _profileFields) {
    const el = document.getElementById(`p-${f.replace(/_/g, "-")}`);
    body[f] = el ? el.value : "";
  }
  try {
    await api("/api/profile", { method: "POST", body: JSON.stringify(body) });
    notify("Profile saved.");
    loadProfile();
  } catch (err) { notifyError(err); }
});

// --- user YOE ---
async function loadUserYoe() {
  try {
    const r = await api("/api/user-yoe");
    const el = $("#user-yoe-input");
    if (el && r.user_yoe != null) el.value = r.user_yoe;
    $("#user-yoe-status").textContent = r.user_yoe != null ? `Saved: ${r.user_yoe}` : "(not set)";
  } catch { /* optional */ }
}
$("#yoe-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#user-yoe-input").value;
  try {
    await api("/api/user-yoe", { method: "POST", body: JSON.stringify({ user_yoe: v === "" ? null : Number(v) }) });
    loadUserYoe();
  } catch (err) { notifyError(err); }
});

// --- style sample ---
async function loadStyle() {
  try {
    const r = await api("/api/meta-kv?k=user_email_style");
    $("#style-input").value = r.v || "";
    $("#style-status").textContent = (r.v && r.v.length) ? `Saved (${r.v.length} chars)` : "(not set)";
  } catch { /* optional */ }
}
$("#style-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/meta-kv", { method: "POST", body: JSON.stringify({ k: "user_email_style", v: $("#style-input").value }) });
    loadStyle();
  } catch (err) { notifyError(err); }
});

// --- saved answers: read-only review of the answers file ---
// Values whose path looks like a credential or ID number are never shown.
const SECRET_PATH_RE = /(pass(word|code)?|secret|token|api[_-]?key|otp|\bpin\b|ssn|aadha+r|\bpan\b|pan_(number|no)|passport_(number|no)|account_(number|no)|iban|ifsc|cvv|card_number)/i;
let reviewRows = [];
let reviewFilter = "all";

function fmtAnswerValue(path, v) {
  if (SECRET_PATH_RE.test(path)) return { text: "hidden", secret: true };
  if (v == null || v === "") return { text: "—", secret: false };
  if (typeof v === "boolean") return { text: v ? "Yes" : "No", secret: false };
  if (Array.isArray(v)) return { text: v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(", "), secret: false };
  if (typeof v === "object") return { text: JSON.stringify(v), secret: false };
  return { text: String(v), secret: false };
}

async function loadReview() {
  const box = $("#review-list");
  box.innerHTML = '<div class="hint">Loading…</div>';
  try {
    const r = await api("/api/answers/review");
    reviewRows = r.rows || [];
    const c = r.counts || {};
    $("#review-counts").textContent = `${c.approved || 0} approved · ${c.proposed || 0} need review · ${c.empty || 0} empty`;
    // Show only the file name, never a full local path.
    const name = r.path ? String(r.path).split(/[\\/]/).pop() : "";
    $("#review-path").textContent = name ? ` (${name})` : "";
    renderReview();
  } catch (e) {
    box.innerHTML = errorHtml(e);
  }
}

function renderReview() {
  $$("#review-filter .chip").forEach((b) => b.classList.toggle("active", b.dataset.filter === reviewFilter));
  const rows = reviewRows.filter((r) => reviewFilter === "all"
    || (reviewFilter === "empty" ? r.empty : (!r.empty && r.status === reviewFilter)));
  const box = $("#review-list");
  if (!rows.length) { box.innerHTML = emptyHtml(reviewRows.length ? "Nothing in this view." : "No saved answers found."); return; }
  box.innerHTML = `<table class="review">
    <thead><tr><th>Field</th><th>Value</th><th>Status</th></tr></thead>
    <tbody>${rows.map((r) => {
      const v = fmtAnswerValue(r.path || "", r.value);
      const st = r.empty ? "empty" : (r.status || "");
      return `<tr>
        <td class="r-key">${escapeHtml(r.path)}${r.derived ? ' <span class="hint">(derived)</span>' : ""}</td>
        <td class="r-val ${v.secret ? "secret" : ""}">${escapeHtml(v.text)}</td>
        <td><span class="pill pill-${escapeHtml(st)}">${escapeHtml(st === "proposed" ? "needs review" : st || "—")}</span></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

// --- quick answers (answer pack) ---
async function loadAnswers() {
  const div = $("#answers-list");
  try {
    const r = await api("/api/answer-pack");
    if (!r.items.length) { div.innerHTML = emptyHtml("No quick answers yet."); return; }
    div.innerHTML = r.items.map((it) => `
      <div class="ap-row">
        <div class="ap-q">${escapeHtml(it.q)}</div>
        <div class="ap-a">${escapeHtml(it.a)}</div>
        ${DEMO ? "<span></span>" : `<button class="ghost btn-sm" data-action="answer-del" data-q="${escapeHtml(it.q)}">Delete</button>`}
      </div>`).join("");
  } catch (e) {
    div.innerHTML = errorHtml(e);
  }
}
$("#answer-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#answer-q").value.trim(), a = $("#answer-a").value.trim();
  if (!q || !a) return;
  try {
    await api("/api/answer-pack", { method: "POST", body: JSON.stringify({ q, a }) });
    $("#answer-q").value = ""; $("#answer-a").value = "";
    loadAnswers();
  } catch (err) { notifyError(err); }
});
async function deleteAnswer(q) {
  try {
    await api("/api/answer-pack?q=" + enc(q), { method: "DELETE" });
    loadAnswers();
  } catch (e) { notifyError(e); }
}

// --- email drafter modal ---
async function openEmailModal(key) {
  $("#modal-title").textContent = "Draft email";
  $("#modal-body").innerHTML = '<div class="hint">Drafting (LLM call)…</div>';
  $("#modal-approve").textContent = "Copy mailto";
  $("#modal-approve").disabled = true;
  showModal();
  let r;
  try {
    r = await api(`/api/jobs/${enc(key)}/draft-email`, { method: "POST", body: "{}" });
  } catch (e) {
    $("#modal-body").innerHTML = errorHtml(e);
    return;
  }
  const g = r.grounding || {};
  const flags = !g.clean ? groundingWarnHtml(g, "confirm-grounding-em", "⚠ Grounding flagged:") : "";
  $("#modal-body").innerHTML = `
    ${flags}
    <p><b>To:</b> ${escapeHtml(r.to || "")}</p>
    <p><b>Subject:</b> <input id="em-subject" class="wide"/></p>
    <textarea id="em-body" class="wide" rows="14"></textarea>
    <p class="hint">Remember to attach your tailored PDF before sending.</p>
  `;
  // Model output goes in through .value (never parsed as HTML).
  $("#em-subject").value = r.subject || "";
  $("#em-body").value = r.body || "";
  const setOk = () => $("#modal-approve").disabled = !(g.clean || $("#confirm-grounding-em")?.checked);
  setOk();
  const cb = $("#confirm-grounding-em"); if (cb) cb.addEventListener("change", setOk);
  $("#modal-approve").onclick = () => {
    const subj = enc($("#em-subject").value);
    const body = enc($("#em-body").value);
    const to = enc(r.to || "");
    window.location.href = `mailto:${to}?subject=${subj}&body=${body}`;
  };
}

// --- copy-for-application hand-off ---
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const original = btn.textContent;
      btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = original), 1200);
    }
  } catch (e) {
    notify("Copy failed — your browser blocked clipboard access.", { kind: "error" });
  }
}

// Step 1: Tailor & prepare → ranked resume picker.
async function openApplyPicker(key) {
  $("#modal-title").textContent = "Tailor & prepare · choose a résumé";
  $("#modal-body").innerHTML = '<div class="hint">Scoring your résumés against this job…</div>';
  $("#modal-approve").textContent = "Close";
  $("#modal-approve").disabled = false;
  $("#modal-approve").onclick = hideModal;
  showModal();

  let r;
  try {
    r = await api(`/api/jobs/${enc(key)}/resume-options`);
  } catch (e) {
    $("#modal-body").innerHTML = errorHtml(e);
    return;
  }
  const threshold = Math.round((r.reuse_threshold || 0.85) * 100);
  const rows = (r.options || []).map((o) => {
    const source = o.source === "version" ? `version:${o.version_id}` : o.source;
    const cov = o.coverage_pct == null
      ? `<span class="hint">(new)</span>`
      : `<span class="badge fit">${o.coverage_pct}% coverage</span>`;
    const rec = o.recommended ? `<span class="badge fit">recommended</span>` : "";
    const label = o.source === "tailor_fresh"
      ? "Tailor a fresh version" : (o.label || o.source);
    return `<div class="ap-row">
      <div class="ap-q">${escapeHtml(label)}</div>
      <div class="ap-a">${cov} ${rec}</div>
      <button class="btn-secondary btn-sm" data-action="pick-resume"
              data-key="${escapeHtml(key)}"
              data-source="${escapeHtml(source)}">Use this</button>
    </div>`;
  }).join("");
  $("#modal-body").innerHTML = `
    <p class="hint">Ranked by keyword coverage of this job's requirements.
       The best existing résumé is marked "recommended" only if it clears
       ${threshold}%. You always choose.</p>
    ${rows}
  `;
}

async function pickResume(key, source) {
  if (source === "tailor_fresh") {
    // Hand off to the existing tailor flow. On Approve it links the new
    // version to the job, so re-opening apply-pack will surface it.
    openTailorModal(key);
    return;
  }
  openApplyPack(key, source);
}

// Step 2: complete hand-off panel.
async function openApplyPack(key, resumeSelector) {
  $("#modal-title").textContent = "Copy for application";
  $("#modal-body").innerHTML = '<div class="hint">Loading…</div>';
  $("#modal-approve").textContent = "Done";
  $("#modal-approve").disabled = false;
  $("#modal-approve").onclick = hideModal;
  showModal();
  let r;
  try {
    const qs = resumeSelector ? `?resume=${enc(resumeSelector)}` : "";
    r = await api(`/api/jobs/${enc(key)}/apply-pack${qs}`);
  } catch (e) {
    $("#modal-body").innerHTML = errorHtml(e);
    return;
  }

  const cr = r.chosen_resume || {};
  const resumeBlock = cr.source
    ? `<div class="ap-row">
         <div class="ap-q">Résumé</div>
         <div class="ap-a">
           <b>${escapeHtml(cr.label || "")}</b>
           ${cr.filename ? `<br/><span class="hint">${escapeHtml(cr.filename)}</span>` : ""}
           ${cr.pdf_path ? `<br/><code>${escapeHtml(cr.pdf_path)}</code>` : ""}
           ${cr.note ? `<br/><span class="hint">${escapeHtml(cr.note)}</span>` : ""}
         </div>
         <div>
           ${cr.pdf_url ? linkHtml(cr.pdf_url, "⬇ PDF", 'class="btn-primary" target="_blank" rel="noopener" download') : ""}
           ${cr.pdf_path ? `<button class="btn-secondary btn-sm" data-action="copy" data-text="${escapeHtml(cr.pdf_path)}">Copy path</button>` : ""}
         </div>
       </div>`
    : `<div class="hint">No résumé selected.</div>`;

  const profile = r.profile || {};
  const profileFields = [
    ["First name", profile.first_name],
    ["Last name", profile.last_name],
    ["Email", profile.email],
    ["Phone", profile.phone],
    ["LinkedIn", profile.linkedin_url],
    ["Portfolio / GitHub", profile.portfolio_url],
  ];
  const profileRows = profileFields.map(([label, val]) => {
    const safe = val || "";
    return `<div class="ap-row">
      <div class="ap-q">${label}</div>
      <div class="ap-a">${safe ? escapeHtml(safe) : '<span class="hint">not set on the Résumés tab</span>'}</div>
      ${safe ? `<button class="btn-secondary btn-sm" data-action="copy" data-text="${escapeHtml(safe)}">Copy</button>` : `<span></span>`}
    </div>`;
  }).join("");

  const answerRows = (r.answer_pack || []).map((it) => `
    <div class="ap-row">
      <div class="ap-q">${escapeHtml(it.q)}</div>
      <div class="ap-a">${escapeHtml(it.a)}</div>
      <button class="btn-secondary btn-sm" data-action="copy" data-text="${escapeHtml(it.a)}">Copy</button>
    </div>`).join("");

  const postingLink = r.posting_url
    ? `<p>${linkHtml(r.posting_url, "↗ Open posting in a new tab", 'class="btn-secondary" target="_blank" rel="noopener noreferrer"')}</p>`
    : '<p class="hint">No apply URL on this posting.</p>';

  $("#modal-body").innerHTML = `
    ${postingLink}
    <h4>Résumé</h4>
    ${resumeBlock}
    <h4>Profile</h4>
    ${profileRows}
    <h4>Quick answers</h4>
    ${answerRows || '<div class="hint">Empty — add entries on the Saved answers tab.</div>'}
    <p class="hint">Click Copy next to any field, then paste into the application form. No auto-fill by design.</p>
    <p>${statusBtn(key, "submitted", "✓ I submitted it", "", "btn-primary")}</p>
  `;
}

// Demo mode: switch off controls the demo server refuses (it only allows
// tracker status changes and UI preferences), instead of letting them fail.
const DEMO_LOCKED = ["#run-daily", "#run-ingest", "#run-score",
  "#resume-form button[type=submit]", "#resume-form input[type=file]",
  "#profile-form button[type=submit]", "#answer-form button[type=submit]"];
function applyDemoLocks() {
  if (!DEMO) return;
  for (const sel of DEMO_LOCKED) {
    const el = $(sel);
    if (!el) continue;
    el.disabled = true;
    el.title = "Not available in the public demo — run Job-Copilot locally to use this";
    if (el.parentElement) el.parentElement.title = el.title;
  }
}

// --- boot ---
try {
  applyDemoLocks();
  loadMeta();
  loadLlmStatus();
  loadRunStatus();
  const initial = (window.location.hash || "").replace("#", "");
  showTab(TAB_LOADERS[initial] ? initial : "today");
  setInterval(loadLlmStatus, 30000);
} catch (e) {
  console.error("[app.js] boot failed:", e);
}
