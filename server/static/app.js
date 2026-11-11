const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// --- tabs ---
$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "tracker") renderBoard();
    if (btn.dataset.tab === "resume") { loadVersions(); loadUserYoe(); loadStyle(); loadProfile(); }
    if (btn.dataset.tab === "answers") loadAnswers();
  });
});

// --- helpers ---
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const t = await res.text();
    const err = new Error(`${res.status}: ${t}`);
    // Callers that need to react to a specific failure (e.g. the 409 from the
    // server-side grounding gate) read these; everyone else just uses .message.
    err.status = res.status;
    try { err.payload = JSON.parse(t); } catch { err.payload = null; }
    throw err;
  }
  return res.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(parseFloat(ts) * 1000);
  return d.toLocaleString();
}

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

// --- card builder (no inline handlers; data-* attrs only) ---
function jobCard(j) {
  const pay = j.comp_max
    ? `~${Math.round((j.comp_max * (j.comp_currency === "USD" ? 83 : 1)) / 100000)} LPA`
    : j.package_estimated ? "est." : "—";
  const geo = (j.country_bucket || "?") + (j.country_uncertain ? "?" : "");
  const ageBadge = j.freshness
    ? `<span class="badge ${escapeHtml(j.freshness)}">${escapeHtml(j.posted_age_days ?? "?")}d · ${escapeHtml(j.freshness)}</span>`
    : "";
  const scoreBadge = j.match_score != null
    ? `<span class="badge score">match ${j.match_score.toFixed(1)}</span>` : "";
  const atsBadge = j.ats_coverage != null
    ? `<span class="badge ats">ATS ${Math.round(j.ats_coverage * 100)}%</span>` : "";
  const senBadge = j.seniority_high ? `<span class="badge high-sen">senior+</span>` : "";

  const fitBadge = j.fit_score != null ? `<span class="badge fit">fit ${escapeHtml(j.fit_score)}</span>` : "";
  const lowConfBadge = j.ats_low_confidence ? `<span class="badge lowconf">low signal · ${j.required_keywords.length} kw</span>` : "";
  const yoeBadge = j.min_yoe_required != null
    ? `<span class="badge ${j.yoe_gap ? 'yoe-gap' : (j.yoe_is_preferred ? 'yoe-soft' : 'yoe-req')}">YOE ≥ ${escapeHtml(j.min_yoe_required)}${j.yoe_is_preferred ? ' (pref)' : ''}${j.yoe_gap ? ' · gap' : ''}</span>`
    : "";

  const missing = Array.isArray(j.missing_keywords) ? j.missing_keywords : [];
  const required = Array.isArray(j.required_keywords) ? j.required_keywords : [];
  const missingRow = required.length
    ? `<div class="job-keywords">
         <span class="hint">required (${required.length}):</span>
         ${required.map((k) => {
           const have = !missing.includes(k.toLowerCase());
           return `<span class="kw ${have ? "have" : "miss"}">${escapeHtml(k)}</span>`;
         }).join(" ")}
       </div>` : "";

  const hasUrl = typeof j.url === "string" && j.url.trim().length > 0;
  const openBtn = hasUrl
    ? linkHtml(j.url, "open posting", 'class="btn-link" target="_blank" rel="noopener noreferrer"')
    : `<button class="btn-link" disabled title="no apply URL on this posting">open posting</button>`;

  const k = escapeHtml(j.key);
  const div = document.createElement("div");
  div.className = "job";
  div.dataset.key = j.key;
  div.innerHTML = `
    <div class="job-row1">
      <span class="job-company">${escapeHtml(j.company)}</span>
      <span class="job-title">${escapeHtml(j.title)}</span>
      ${senBadge}
    </div>
    <div class="job-meta">
      ${escapeHtml(j.role_family || "?")} · ${escapeHtml(j.workplace_type)} · ${escapeHtml(geo)} ·
      pay ${pay} · ${ageBadge} ${fitBadge} ${scoreBadge} ${atsBadge} ${lowConfBadge} ${yoeBadge}
      · status: <b>${escapeHtml(j.status)}</b>
    </div>
    ${missingRow}
    <div class="job-actions">
      ${openBtn}
      <button data-action="tailor" data-key="${k}">Tailor</button>
      <button data-action="apply-job" data-key="${k}" class="btn-primary">Apply</button>
      ${j.apply_method === "email" ? `<button data-action="draft-email" data-key="${k}">Draft email</button>` : ""}
      <button data-action="status" data-key="${k}" data-status="interested">Interested</button>
      <button data-action="status" data-key="${k}" data-status="not_interested">Not interested</button>
      <button data-action="status" data-key="${k}" data-status="applied">Mark applied</button>
    </div>
  `;
  return div;
}

// --- delegated click dispatch ---
async function handleStatusClick(target) {
  const key = target.dataset.key;
  const status = target.dataset.status;
  if (!key || !status) return;
  target.disabled = true;
  try {
    await api(`/api/jobs/${encodeURIComponent(key)}/status`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    // refresh whichever panel is currently visible
    if ($("#tab-tracker").classList.contains("active")) renderBoard();
    else loadJobs();
    loadMeta();
  } catch (e) {
    alert(e.message);
    target.disabled = false;
  }
}

// Single, document-level delegated click listener. `document` is always
// present (unlike tab containers that could in theory be missing), so this
// handler is guaranteed to be attached before any card is rendered.
let _clickLoggedOnce = false;
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (!_clickLoggedOnce) {
    console.debug("[app.js] first delegated click:", action, btn.dataset);
    _clickLoggedOnce = true;
  }
  try {
    if (action === "status") handleStatusClick(btn);
    else if (action === "apply") loadJobs();
    else if (action === "tailor") openTailorModal(btn.dataset.key);
    else if (action === "draft-email") openEmailModal(btn.dataset.key);
    else if (action === "export-csv") downloadCsv();
    else if (action === "answer-del") deleteAnswer(btn.dataset.q);
    else if (action === "copy") copyText(btn.dataset.text, btn);
    else if (action === "apply-job") openApplyPicker(btn.dataset.key);
    else if (action === "apply-pack") openApplyPack(btn.dataset.key);
    else if (action === "pick-resume") pickResume(btn.dataset.key, btn.dataset.source);
  } catch (e) {
    console.error("[app.js] click handler threw:", e);
    alert("Action failed (see console): " + e.message);
  }
});

// Kept for backwards-compat with the couple of remaining call sites; now
// a no-op because delegation lives on `document`.
function attachDelegation(_) { /* no-op */ }

// Enter inside any filter input also applies.
try {
  $$("#tab-browse .filters input").forEach((i) => {
    i.addEventListener("keydown", (e) => { if (e.key === "Enter") loadJobs(); });
  });
} catch (e) { console.warn("[app.js] filter Enter binding skipped:", e); }

// --- browse ---
async function loadJobs() {
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

  const list = $("#jobs-list");
  list.innerHTML = '<div class="hint">loading…</div>';
  try {
    const data = await api("/api/jobs?" + params.toString());
    list.innerHTML = "";
    if (!data.jobs.length) {
      list.innerHTML = '<div class="hint">no jobs match. Run ingest, or relax filters.</div>';
      return;
    }
    for (const j of data.jobs) list.appendChild(jobCard(j));
  } catch (e) {
    list.innerHTML = `<div class="hint">error: ${escapeHtml(e.message)}</div>`;
  }
}

// --- tracker ---
async function renderBoard() {
  const cols = ["new", "interested", "ready", "applied", "rejected"];
  const labels = { new: "New", interested: "Interested", ready: "Ready", applied: "Applied", rejected: "Rejected" };
  const board = $("#board");
  board.innerHTML = "";
  for (const c of cols) {
    const col = document.createElement("div");
    col.className = "column";
    col.innerHTML = `<h3>${labels[c]}</h3>`;
    board.appendChild(col);
    try {
      const data = await api(`/api/jobs?status=${c}&limit=100`);
      for (const j of data.jobs) col.appendChild(jobCard(j));
    } catch (e) {
      col.innerHTML += `<div class="hint">${escapeHtml(e.message)}</div>`;
    }
  }
}

// --- ingest button ---
$("#run-score").addEventListener("click", async () => {
  const btn = $("#run-score");
  btn.disabled = true; btn.textContent = "Scoring…";
  try {
    const res = await api("/api/score", { method: "POST", body: "{}" });
    alert(`Scored ${res.scored} jobs (resume keywords: ${res.resume_keyword_count})`);
    await loadJobs();
  } catch (e) {
    alert("Score failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Score all";
  }
});

$("#run-ingest").addEventListener("click", async () => {
  const btn = $("#run-ingest");
  btn.disabled = true; btn.textContent = "Running…";
  try {
    const res = await api("/api/ingest", { method: "POST", body: "{}" });
    alert(`Ingest done: ${res.ingested_raw} raw → ${res.passed} kept (${res.inserted} new, ${res.updated} updated)`);
    await loadMeta();
    await loadJobs();
  } catch (e) {
    alert("Ingest failed: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "Run ingest";
  }
});

// --- resume ---
$("#resume-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const res = await fetch("/api/resume", { method: "POST", body: fd });
  if (!res.ok) { alert("upload failed"); return; }
  await loadMeta();
});

async function loadVersions() {
  try {
    const data = await api("/api/resume-versions");
    const div = $("#versions-list");
    if (!data.versions.length) {
      div.textContent = "none yet (Phase E)";
      return;
    }
    div.innerHTML = data.versions.map((v) =>
      `<div class="job"><b>${escapeHtml(v.label)}</b> · ${escapeHtml(v.role_family || "")} ·
       ATS ${Math.round((v.base_ats_coverage || 0) * 100)}%
       ${v.pdf_path ? linkHtml(`/api/resume-versions/${encodeURIComponent(v.id)}/download`, "PDF", 'target="_blank" rel="noopener"') : ""}</div>`
    ).join("");
  } catch {}
}

// --- meta header ---
async function loadMeta() {
  try {
    const m = await api("/api/meta");
    $("#last-ingest").textContent = "last run: " + fmtTs(m.last_ingest);
    $("#resume-status").textContent = m.resume_loaded
      ? `resume: ${m.resume_filename || "loaded"}`
      : "resume: not uploaded";
    if (m.resume_loaded) {
      $("#resume-info").textContent = `loaded: ${m.resume_filename}`;
    }
  } catch {}
}

// --- LLM badge ---
async function loadLlmStatus() {
  const el = $("#llm-status");
  if (!el) return;
  try {
    const h = await api("/api/llm/health");
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

async function openTailorModal(key) {
  tailorState.key = key;
  $("#modal-title").textContent = "Tailor";
  $("#modal-body").innerHTML = '<div class="hint">checking for reusable versions…</div>';
  $("#modal-approve").disabled = true;
  $("#modal-status").textContent = "";
  showModal();

  let suggestion;
  try {
    suggestion = await api(`/api/jobs/${encodeURIComponent(key)}/tailor-suggestion`);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">error: ${escapeHtml(e.message)}</div>`;
    return;
  }
  tailorState.suggestion = suggestion;

  if (suggestion.mode === "reuse") {
    const v = suggestion.version;
    $("#modal-body").innerHTML = `
      <p>A stored version already covers
         <b>${Math.round(suggestion.coverage * 100)}%</b> of this job's keywords.</p>
      <div class="job">
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
        const res = await api(`/api/jobs/${encodeURIComponent(key)}/approve`, {
          method: "POST", body: JSON.stringify(body),
        });
        alert(`Linked to version ${res.version_id}. Status: ready.`);
        hideModal(); loadJobs();
      } catch (e) {
        if (e.status === 409 && e.payload) {
          // The server re-checked the stored text against your CURRENT resume.
          // Nothing was linked; show what it found and require confirmation.
          $("#modal-status").textContent =
            e.payload.detail || "Grounding check failed on the server. Nothing was linked.";
          if (!e.payload.grounding) { $("#modal-approve").disabled = true; return; }
          reuseGrounding = e.payload.grounding;
          $("#reuse-grounding").innerHTML = groundingWarnHtml(reuseGrounding, "confirm-grounding-reuse");
          $("#modal-approve").disabled = true;
          $("#confirm-grounding-reuse").addEventListener("change", (ev) => {
            $("#modal-approve").disabled = !ev.target.checked;
          });
          return;
        }
        alert(e.message);
      }
    };
    return;
  }

  // Fresh path: call /tailor (this is the only LLM call in the flow)
  $("#modal-body").innerHTML = '<div class="hint">tailoring (this may take 10–60s)…</div>';
  let t;
  try {
    t = await api(`/api/jobs/${encodeURIComponent(key)}/tailor`, {
      method: "POST", body: JSON.stringify({}),
    });
  } catch (e) {
    const msg = escapeHtml(e.message);
    const llmBadge = $("#llm-status");
    const llmOk = llmBadge && llmBadge.classList.contains("llm-ok");
    const hint = llmOk
      ? "The LLM is reachable (badge is green). This is usually a malformed-JSON reply from the small local model. Consider LLM_PROVIDER=groq (free tier, stronger model) — see README."
      : "The LLM badge is not green. Start Ollama or set LLM_PROVIDER in .env.";
    $("#modal-body").innerHTML = `<div class="hint">tailor failed: ${msg}</div><div class="hint">${hint}</div>`;
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
      const res = await api(`/api/jobs/${encodeURIComponent(tailorState.key)}/approve`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      alert(`Saved version ${res.version_id} (ATS ${Math.round(res.new_ats_coverage * 100)}%). Status: ready.`);
      hideModal();
      loadJobs();
      loadVersions();
    } catch (e) {
      if (e.status === 409 && e.payload && e.payload.grounding) {
        // The server found flags not confirmed here (e.g. the resume or job
        // keywords changed since tailoring). Show its findings and require a
        // fresh confirmation; nothing was saved.
        t.grounding = e.payload.grounding;
        renderTailorResult(t);
        $("#modal-status").textContent =
          e.payload.detail || "Grounding check failed on the server. Nothing was saved.";
        return;
      }
      alert(e.message);
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
    const s = $("#profile-status");
    if (s) s.textContent = `${filled}/${_profileFields.length} fields set`;
  } catch {}
}

const profileForm = $("#profile-form");
if (profileForm) profileForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const f of _profileFields) {
    const el = document.getElementById(`p-${f.replace(/_/g, "-")}`);
    body[f] = el ? el.value : "";
  }
  await api("/api/profile", { method: "POST", body: JSON.stringify(body) });
  loadProfile();
});

// --- user YOE ---
async function loadUserYoe() {
  try {
    const r = await api("/api/user-yoe");
    const el = $("#user-yoe-input");
    if (el && r.user_yoe != null) el.value = r.user_yoe;
    const s = $("#user-yoe-status");
    if (s) s.textContent = r.user_yoe != null ? `saved: ${r.user_yoe}` : "(not set)";
  } catch {}
}
const yoeForm = $("#yoe-form");
if (yoeForm) yoeForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#user-yoe-input").value;
  await api("/api/user-yoe", { method: "POST", body: JSON.stringify({ user_yoe: v === "" ? null : Number(v) }) });
  loadUserYoe(); loadJobs();
});

// --- style sample ---
async function loadStyle() {
  try {
    const r = await api("/api/meta-kv?k=user_email_style");
    const el = $("#style-input");
    if (el) el.value = r.v || "";
    const s = $("#style-status"); if (s) s.textContent = (r.v && r.v.length) ? `saved (${r.v.length} chars)` : "(not set)";
  } catch {}
}
const styleForm = $("#style-form");
if (styleForm) styleForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/meta-kv", { method: "POST", body: JSON.stringify({ k: "user_email_style", v: $("#style-input").value }) });
  loadStyle();
});

// --- answer pack ---
async function loadAnswers() {
  try {
    const r = await api("/api/answer-pack");
    const div = $("#answers-list");
    if (!r.items.length) { div.innerHTML = '<div class="hint">empty</div>'; return; }
    div.innerHTML = r.items.map(it => `
      <div class="job">
        <div><b>${escapeHtml(it.q)}</b></div>
        <div>${escapeHtml(it.a)}</div>
        <div class="job-actions">
          <button class="ghost" data-action="answer-del" data-q="${escapeHtml(it.q)}">Delete</button>
        </div>
      </div>`).join("");
  } catch {}
}
const ansForm = $("#answer-form");
if (ansForm) ansForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#answer-q").value.trim(), a = $("#answer-a").value.trim();
  if (!q || !a) return;
  await api("/api/answer-pack", { method: "POST", body: JSON.stringify({ q, a }) });
  $("#answer-q").value = ""; $("#answer-a").value = "";
  loadAnswers();
});
async function deleteAnswer(q) {
  await api("/api/answer-pack?q=" + encodeURIComponent(q), { method: "DELETE" });
  loadAnswers();
}
attachDelegation("#tab-answers");

// --- CSV download (respects active filters) ---
function downloadCsv() {
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
  window.open("/api/export.csv?" + params.toString(), "_blank");
}

// --- email drafter modal ---
async function openEmailModal(key) {
  $("#modal-title").textContent = "Draft email";
  $("#modal-body").innerHTML = '<div class="hint">drafting (LLM call) …</div>';
  $("#modal-approve").textContent = "Copy mailto";
  $("#modal-approve").disabled = true;
  showModal();
  let r;
  try {
    r = await api(`/api/jobs/${encodeURIComponent(key)}/draft-email`, { method: "POST", body: "{}" });
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">draft failed: ${escapeHtml(e.message)}</div>`;
    return;
  }
  const g = r.grounding || {};
  const flags = !g.clean ? groundingWarnHtml(g, "confirm-grounding-em", "⚠ Grounding flagged:") : "";
  $("#modal-body").innerHTML = `
    ${flags}
    <p><b>To:</b> ${escapeHtml(r.to || "")}</p>
    <p><b>Subject:</b> <input id="em-subject"/></p>
    <textarea id="em-body" rows="14"></textarea>
    <p class="hint">Remember to attach your tailored PDF before sending.</p>
  `;
  // Model output goes in through .value (never parsed as HTML); sizes via
  // CSSOM because the CSP blocks inline style attributes.
  $("#em-subject").value = r.subject || "";
  $("#em-subject").style.width = "80%";
  $("#em-body").value = r.body || "";
  $("#em-body").style.width = "100%";
  const setOk = () => $("#modal-approve").disabled = !(g.clean || $("#confirm-grounding-em")?.checked);
  setOk();
  const cb = $("#confirm-grounding-em"); if (cb) cb.addEventListener("change", setOk);
  $("#modal-approve").onclick = () => {
    const subj = encodeURIComponent($("#em-subject").value);
    const body = encodeURIComponent($("#em-body").value);
    const to = encodeURIComponent(r.to || "");
    window.location.href = `mailto:${to}?subject=${subj}&body=${body}`;
  };
}

// --- copy-for-application hand-off ---
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const original = btn.textContent;
      btn.textContent = "copied ✓";
      setTimeout(() => (btn.textContent = original), 1200);
    }
  } catch (e) {
    alert("Copy failed: " + e.message);
  }
}

// Step 1: Apply → ranked resume picker.
async function openApplyPicker(key) {
  $("#modal-title").textContent = "Apply · choose a resume";
  $("#modal-body").innerHTML = '<div class="hint">scoring your resumes against this job…</div>';
  $("#modal-approve").textContent = "Close";
  $("#modal-approve").disabled = false;
  $("#modal-approve").onclick = hideModal;
  showModal();

  let r;
  try {
    r = await api(`/api/jobs/${encodeURIComponent(key)}/resume-options`);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">error: ${escapeHtml(e.message)}</div>`;
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
      <button class="btn-sec" data-action="pick-resume"
              data-key="${escapeHtml(key)}"
              data-source="${escapeHtml(source)}">Use this</button>
    </div>`;
  }).join("");
  $("#modal-body").innerHTML = `
    <p class="hint">Ranked by keyword coverage of this job's requirements.
       The best existing resume is marked "recommended" only if it clears
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
  $("#modal-body").innerHTML = '<div class="hint">loading pack…</div>';
  $("#modal-approve").textContent = "Done";
  $("#modal-approve").disabled = false;
  $("#modal-approve").onclick = hideModal;
  showModal();
  let r;
  try {
    const qs = resumeSelector ? `?resume=${encodeURIComponent(resumeSelector)}` : "";
    r = await api(`/api/jobs/${encodeURIComponent(key)}/apply-pack${qs}`);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">error: ${escapeHtml(e.message)}</div>`;
    return;
  }

  const cr = r.chosen_resume || {};
  const resumeBlock = cr.source
    ? `<div class="ap-row">
         <div class="ap-q">Resume</div>
         <div class="ap-a">
           <b>${escapeHtml(cr.label || "")}</b>
           ${cr.filename ? `<br/><span class="hint">${escapeHtml(cr.filename)}</span>` : ""}
           ${cr.pdf_path ? `<br/><code>${escapeHtml(cr.pdf_path)}</code>` : ""}
           ${cr.note ? `<br/><span class="hint">${escapeHtml(cr.note)}</span>` : ""}
         </div>
         <div>
           ${cr.pdf_url ? linkHtml(cr.pdf_url, "⬇ PDF", 'class="btn-primary" target="_blank" rel="noopener" download') : ""}
           ${cr.pdf_path ? `<button class="btn-sec" data-action="copy" data-text="${escapeHtml(cr.pdf_path)}">Copy path</button>` : ""}
         </div>
       </div>`
    : `<div class="hint">no resume selected</div>`;

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
      <div class="ap-a">${safe ? escapeHtml(safe) : '<span class="hint">not set on Resume tab</span>'}</div>
      ${safe ? `<button class="btn-sec" data-action="copy" data-text="${escapeHtml(safe)}">Copy</button>` : `<span></span>`}
    </div>`;
  }).join("");

  const answerRows = (r.answer_pack || []).map((it) => `
    <div class="ap-row">
      <div class="ap-q">${escapeHtml(it.q)}</div>
      <div class="ap-a">${escapeHtml(it.a)}</div>
      <button class="btn-sec" data-action="copy" data-text="${escapeHtml(it.a)}">Copy</button>
    </div>`).join("");

  const postingLink = r.posting_url
    ? `<p>${linkHtml(r.posting_url, "↗ Open posting in a new tab", 'class="btn-sec" target="_blank" rel="noopener noreferrer"')}</p>`
    : '<p class="hint">no apply URL on this posting</p>';

  $("#modal-body").innerHTML = `
    ${postingLink}
    <h4>Resume</h4>
    ${resumeBlock}
    <h4>Profile</h4>
    ${profileRows}
    <h4>Answer pack</h4>
    ${answerRows || '<div class="hint">empty — add entries on the Answer Pack tab</div>'}
    <p class="hint">Click Copy next to any field, then paste into the source form. No auto-fill by design (ban-safety).</p>
  `;
}

// --- boot ---
try {
  loadMeta();
  loadLlmStatus();
  loadJobs();
  setInterval(loadLlmStatus, 30000);
} catch (e) {
  console.error("[app.js] boot failed:", e);
}
