# job-copilot — Final build summary (extension-addendum-3, unattended run)

Run completed 2026-06-26. All 5 parts landed, 13/13 tests green, server smoke
test against real APIs successful. Server + Ollama both stopped after this
file was written.

## TL;DR (the two things you asked me to highlight)

### 1. Did Part 2d actually move the company count?

**Yes — from 3 producing companies to 19.**

Verified candidates that returned ≥1 role (live-checked, then appended to
`companies.yaml`):
Stripe (493), Affirm (163), Robinhood (137), Brex (248), Mercury (53),
CRED (10), Postman (121), Datadog (411), Cloudflare (200), Mixpanel (39),
Twilio (163), Asana (140), Notion (147), Linear (23), Ramp (118),
Discord (64), Figma (172), Anthropic (391).

Honest failures (commented out in `companies.yaml`, not silently dropped):
Plaid (404), Razorpay (404), Groww (404), Setu (404), DigitalOcean (404),
HashiCorp (404), Vercel (0 roles), ThoughtSpot (404), Freshworks (404),
Hasura (404). These need ATS-discovery work (likely on Workday/iCIMS or
private boards) before they can be re-tried.

**Live ingest delta:** previously 50 jobs passed all filters from 3
companies. Now **291 jobs passed from 19 producing companies.** The
hide-senior+ toggle now keeps **110 of 301** instead of collapsing to 6 —
exactly the bottleneck you flagged.

### 2. YOE spot-checks against real posting text

Sampled 5 randomly. **4/5 correct.** The one miss is worth knowing about:

| # | Title | Parsed | Source phrase | Verdict |
|---|---|---|---|---|
| 1 | Affirm · Software Engineer II, Backend | 5 | "total of 1.5+ years of experience" | **wrong** — picked a higher bar elsewhere in the body |
| 2 | Asana · Staff Data Scientist | 5 | "5-7 years of data science experience" | correct |
| 3 | Affirm · Staff SWE, Backend | 7 | "7+ years of experience designing, developing…" | correct |
| 4 | Okta · Senior Data Engineer | 5 | "5+ years of professional experience" | correct |
| 5 | Affirm · Senior Manager, SW Engineering | 10 | "10+ years of experience in managing…" | correct |

**Why #1 missed:** the extractor uses the *strictest* required floor when a
posting lists multiple YOE bars (e.g. one for the title role, one for the
team lead they report to). For senior+ postings this matches the user's
intent (don't be misled by a junior bar buried below). For dual-band JD's
it surfaces the wrong number. Three reasonable next steps if this matters:
(a) anchor to YOE matches that appear in a section near the title,
(b) prefer the *lowest* floor when multiple are found and the title looks
junior, or (c) leave as-is and treat the badge as upper-bound. Not changed
in this run — flagged for your call.

The earlier YOE smoke against Okta Staff/Senior titles all matched the
posting text exactly (5+, 7+, 8+, 9+).

---

## What landed, by part

### Part 0 — Workday detail-fetcher
- `WorkdayConnector.fetch_detail(job, company)` (jobcopilot/connectors/workday.py)
- `enrich_workday_descriptions(results, companies, ...)` in ingest.py
  - Per-posting failure isolation (your safety request) — one bad GET logs
    and skips, never aborts the pass. Verified in production: the first
    smoke run hit 60+ wrong-URL 406s and the surrounding code kept running.
  - Per-company delay (`WORKDAY_DETAIL_DELAY_SEC=0.7`) + cap
    (`WORKDAY_DETAIL_MAX=60`) + DB cache (skips keys with non-empty
    description_text).
- Wired into `/api/ingest`: filled descriptions trigger automatic re-score.
- **Bug found & fixed mid-run:** initial URL template had `/job/{external_path}`
  which double-prefixed since externalPath already includes `/job/...`. The
  per-posting isolation contained the blast radius (60+ failures, zero
  cascade) and the fix landed cleanly. Second run: **20 Workday descriptions
  filled + 20 jobs rescored.**

### Part 1 — Multi-factor scoring (no LLM)
- Numeric seniority (filters.py): `L4-9`, `SDE 3-9`,
  `engineer|developer|scientist|sde III|IV|V|VI|3-9`. Conservative — does
  NOT flag `Engineer 2`, `Engineer II`, or `2025` (4-digit-year guard).
  Adobe ML Engineer 4 is now hideable.
- YOE extractor (jobcopilot/yoe.py) with false-positive guards
  (`founded`, `since`, `combined team`, `graduating`). Catches ranges
  ("3-5 years"), preferred-vs-required, and skips year-like numbers.
- Job model: `min_yoe_required`, `yoe_is_preferred` columns added.
  Forward migration in `init_db` (existing DB rows pick up the columns).
- Derived display fields in `store.list_jobs` / `get_job`:
  `fit_score = round(0.5*match + 0.5*ats_pct)`, `ats_low_confidence`
  (when required_keywords < 3), `yoe_gap` (when user_yoe < min_yoe and not
  preferred).
- `/api/user-yoe` GET/POST + Resume-tab input + `max_yoe` query param on
  /api/jobs (off by default — surface, don't gate).
- 5 new tests (YOE corner cases + numeric seniority); all green.

### Part 2 — More sources, funding, more companies
- `connectors/adzuna.py` — fixture + parse test. Skip-live-with-log if
  `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` absent. Not added to companies.yaml
  yet; add a row like `{ name: "Adzuna IN SWE", connector: adzuna,
  what: "software engineer" }` once you have keys.
- `connectors/jsearch.py` — same pattern, discovery-only, needs
  `RAPIDAPI_KEY`. Fixture + parse test green.
- `funding.py` — Inc42/Entrackr/YC RSS → llm.complete → 4-criteria filter
  → writes to `discovered_companies.yaml` (separate from curated). POST
  `/api/funding/run`. Not invoked during this build (Ollama was busy and
  it isn't critical for first usage). Run it manually when ready.
- `scripts/verify_candidates.py` — used to live-verify 28 candidates and
  print yaml. 18 verified, 10 commented as honest failures.

### Part 3 — Polish
- Answer Pack (Resume → new "Answer Pack" tab): `GET/POST/DELETE
  /api/answer-pack`. Stored in `meta.answer_pack` as JSON.
- `GET /api/export.csv` — respects all active filters; "Download CSV"
  button on Browse. 18 columns including fit components, YOE flags,
  status, URL.
- `scripts/com.jobcopilot.refresh.plist.example` — opt-in launchd template
  that POSTs `/api/ingest` every 6 hours. Documented install steps in the
  file header; NOT auto-installed.

### Part 4 — Email drafter (apply_method:email)
- `jobcopilot/email_draft.py` — LLM call with style sample, answer pack,
  tailored resume (if any) all in the prompt. Grounding check applied to
  the email body (a fabricated cover-letter line is worse than a résumé
  one). 4-6 sentence rule baked into the system prompt.
- `POST /api/jobs/{key}/draft-email` — returns `{to, subject, body,
  grounding}`.
- UI: when a card's company has `apply_method: email`, a "Draft email"
  button appears. Modal shows subject + editable body + grounding flags;
  Approve becomes a `mailto:` open (no auto-send).
- `companies.yaml`: BalkanID now has `apply_method: email,
  contact_email: "hiring@balkan.id"`.

---

## What was skipped / left as TODO

- **Funding watcher live run** — code + endpoint shipped, but not invoked
  in this build to keep total runtime under your "few minutes" budget. Hit
  `POST /api/funding/run` (Ollama must be running) when you want it.
- **Adzuna + JSearch live ingest** — code + tests ship, but I didn't add
  search queries to `companies.yaml` because I don't have your keys. Add
  rows + keys to `.env` and they'll work on next ingest.
- **10 candidate companies that 404'd** — left commented in
  `companies.yaml` with the exact error. Most are probably on
  Workday/iCIMS; some (like Plaid) are on private boards.
- **Workday detail-fetcher dual-URL guard** — current code skips one
  posting on a 4xx. Could fall back to fetching the detail HTML page and
  scraping the description, but that crosses the no-scraping line; left as
  manual for now. 20 Workday descriptions did fill on this run.
- **YOE "multiple bars in one JD" tradeoff** — see §2 above. Not fixed.

---

## Stats

| metric | before | after |
|---|---|---|
| unique producing companies | 3 (Okta, PayPal, Adobe) | **19** |
| total jobs in DB | 50 | **301** |
| hide-senior+ count | 6 | **110** |
| Workday descriptions filled | 0 | 20 |
| jobs with parsed YOE | 0 | 211 (70%) |
| tests | 9 | **13** |

## Files added / changed (top-level)

```
jobcopilot/
├── connectors/
│   ├── adzuna.py       (new)
│   ├── jsearch.py      (new)
│   └── workday.py      (+fetch_detail, URL fix)
├── email_draft.py      (new)
├── filters.py          (numeric seniority, YOE pass-through)
├── funding.py          (new)
├── ingest.py           (+enrich_workday_descriptions w/ failure isolation)
├── models.py           (+min_yoe_required, +yoe_is_preferred)
├── scoring.py          (+score_keys)
├── store.py            (+migration, +answer pack, +fit/low-conf/yoe_gap derive)
└── yoe.py              (new)
scripts/
├── verify_candidates.py
└── com.jobcopilot.refresh.plist.example
server/
├── app.py              (~12 new endpoints; CSV; user_yoe; answer pack; email)
└── static/             (badges, modal upgrades, new tabs, max_yoe filter)
tests/
├── fixtures/
│   ├── adzuna.json     (new)
│   └── jsearch.json    (new)
└── test_parsers.py     (+5 new tests)
companies.yaml          (+18 verified, +10 commented failures, BalkanID apply_method)
BUILD_SUMMARY.md        (this file)
```

## Restart (one command)

```bash
cd job-copilot && source .venv/bin/activate
brew services start ollama
OLLAMA_MODEL=llama3.2:3b uvicorn server.app:app --reload    # http://localhost:8000
```

The DB, PDFs, jobs.jsonl, and build/server/jobcopilot logs are all on
disk under `data/` and at the repo root; everything is set up to keep
going where you left off.

---

## Bugfixes pass (2026-07-01)

Five fixes from `bugfixes-and-polish.md`. All 15/15 tests green.

**Fix 1 — Tailor "unparseable JSON":**
- `parse_json` in `llm.py` now strips code fences, salvages the outermost
  `{...}`/`[...]`, drops trailing commas, and balances one missing
  brace/bracket.
- New `llm.complete_json(system, user)` wraps `complete()`: on first parse
  failure it retries once with a stricter reminder appended to the system
  prompt. Both `tailor.py` and `email_draft.py` now go through it.
- The tailor modal's error banner **no longer wrongly blames Ollama when
  the badge is green**. When LLM is reachable and JSON still fails, it says
  "malformed JSON from the small local model. Consider `LLM_PROVIDER=groq`."

**Recommendation:** for the JSON-parse class of error, set
`LLM_PROVIDER=groq` in `.env` (free tier, stronger model, returns clean
JSON). The hardened parser + retry stays as the fallback for fully-local
runs. See `.env.example`.

**Fix 2 — Status buttons on Browse:**
- Single delegated click listener now lives on **`document`** (guaranteed
  to exist) instead of a tab container.
- Boot is wrapped in try/catch; the handler logs the first click to the
  console so any residual issue is diagnosable from DevTools.
- Backend `/api/jobs/{key}/status` verified with `curl`: returns
  `{"ok":true}`.

**Fix 3 — Post-tailor hand-off (NOT auto-fill):**
- New `GET /api/jobs/{key}/apply-pack` returns tailored PDF download URL +
  posting URL + answer-pack items.
- Cards with a linked version show a **"Copy for application"** button.
  Modal offers one-click Copy on every answer + PDF download; the user
  pastes into the source form. Explicit note in the UI about no auto-fill
  (ban-safety).

**Fix 4 — UI polish:**
- Full CSS rewrite (`styles.css`) with a design-system approach: CSS
  variables for palette/spacing/radii, real type hierarchy, primary vs
  secondary button styles, pill-shaped meaningful-color badges (green
  strong, amber ok, red weak/gap), a clean filter toolbar, hover states,
  and responsive breakpoints. Same functionality, deliberate look.

**Fix 5 — File logging:**
- `setup_logging()` now attaches `RotatingFileHandler` + `StreamHandler` to
  the **parent `jobcopilot` logger**, so `scoring`, `tailor`, `llm`,
  `email`, and `ingest` all inherit via propagation. Log level = `INFO`.
- Verified: `data/jobcopilot.log` gets a `logging initialized · file=…`
  entry on server startup; subsequent ingest/score/tailor calls append.

**Tests added:** `test_parse_json_salvages_trailing_comma_and_fences`,
`test_logging_setup_attaches_to_parent`. Full suite: **15/15 green.**

---

## Phase Autofill · Step 1 (2026-07-02)

Scoped first half of `phase-autofill.md`. **No browser automation** — that is
the deferred step 2, intentionally kept separate until this foundation is
used on real applications.

### What landed
- **Profile store + form.** New `first_name`, `last_name`, `email`, `phone`,
  `linkedin_url`, `portfolio_url` fields, JSON-encoded in the `meta` table.
  `GET/POST /api/profile`. Grid form on the Resume tab persists across runs.
- **Resume options endpoint.** `GET /api/jobs/{key}/resume-options` returns
  the ranked list: Original + every stored `resume_version`, ordered by
  keyword coverage of `job.required_keywords`. The single best existing
  option is marked `recommended` **only if** its coverage ≥
  `REUSE_THRESHOLD` (0.85 by default). A `tailor_fresh` option is always
  appended.
- **Extended apply-pack.** `GET /api/jobs/{key}/apply-pack?resume=<source>`
  accepts `original` or `version:<id>` and returns the chosen resume's
  filename + download URL + on-disk path (so "Copy path" works), every
  profile field, every answer-pack item, and the posting URL. If a version
  has no PDF on disk (edge case), the `note` field flags it honestly.
- **Apply picker modal.** New primary **Apply** button on every card
  (`data-action="apply-job"`, distinct from the filter "Apply"). Opens a
  ranked picker; the user always chooses (recommendation is a default, not
  forced). Picking a stored resume → hand-off panel. Picking
  `tailor_fresh` → the existing tailor flow (grounding + review + approve)
  → then re-open with the newly linked version.
- **Hand-off panel (this is the actual deliverable).** Every field —
  chosen resume path, each profile field, each answer-pack answer — has a
  one-click Copy button. Posting opens in a new tab. Explicit note about
  no auto-fill (ban-safety).

### What's explicitly NOT here (step 2 territory)
- No Playwright, no `playwright install`, no browser launch, no ATS
  selector maps. Everything above is text and copy-paste; the browser
  layer is the deferred follow-up, to be prioritized by whichever ATS the
  user actually applies to most.

### One real bug caught during the smoke test
My first smoke test showed pytest data leaking into the real DB
(`greenhouse:Foo:1` job + two versions with `/pytest-of-…/` paths). Root
cause: `store.*(db_path=DEFAULT_DB_PATH)` binds the default at function-def
time, so `monkeypatch.setattr(store, "DEFAULT_DB_PATH", …)` doesn't
sandbox writes. **Fixed both:** the test now passes `db_path=db` explicitly
to every store call, and I removed the leaked rows from the real DB.
Test suite still 17/17 green with real isolation verified.

**Notes for you:**
- The leaked test wrote a placeholder resume text over yours; re-upload
  your real PDF on the Resume tab. The profile the smoke test filled in
  has your real email but placeholder LinkedIn/GitHub URLs (`.../in/x`,
  `.../x`) — update those on the same tab.
- Two new tests: `test_resume_options_ranking_and_recommended_gate`,
  `test_profile_and_answer_pack_roundtrip`. Full suite: **17/17 green.**

