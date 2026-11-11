# Job-Copilot v2 — Plan

Status: **Phase 0 in progress** (go-ahead + answers received 2026-11-11 — see §6).
Baseline: `main @ cb54d88` (identical to GitHub `origin/main`), 27/27 tests green.
Work happens on branch `v2`, one commit per phase. Nothing is pushed unless you say so.
This file and `docs/` are committed to a **public** repo, so they hold no personal data. Personal data lives in the git-ignored `profile/`.

Research behind this plan: [`docs/SOURCES.md`](docs/SOURCES.md) (access rules per source, ATS map) · [`docs/LEARNINGS.md`](docs/LEARNINGS.md) (OSS study; `L#n` below = its ranked idea n).

---

## 1. Current architecture (v1)

1. **Entry points**: `run.py` → `jobcopilot.ingest.main` (CLI → `jobs.jsonl`). `uvicorn server.app:app` runs FastAPI on :8000, serving REST plus a vanilla-JS UI (Browse / Tracker / Resume / Answer Pack).
2. **Config**: `companies.yaml` (37 rows, 19 producing). Each row has `connector` + `slug`/`tenant`; proprietary portals are `manual`. The LLM is set in `.env`, which doesn't exist on disk, so it defaults to Ollama.
3. **Connectors** (`jobcopilot/connectors/`): `fetch_raw()` plus a pure `parse()` → `Job` dataclass. Greenhouse, Lever, Ashby (+comp), Workday (CXS + detail), Adzuna, JSearch (key-gated, never run). Registered in the `REGISTRY` dict.
4. **HTTP**: bare `requests` with a spoofed Chrome UA. No robots.txt check, no shared rate limiter (only Workday's fixed 0.7 s delay).
5. **Filter** (`filters.py`), in order:
   - title allowlist → seniority flag → YOE regex (strictest floor)
   - sector → geo (India: any work type; outside India: remote only; unknown country: kept)
   - one INR floor: 15 LPA (USD converted at a flat 83; EUR unsupported)
6. **Store** (`store.py`): SQLite `jobs`, keyed `source:company:external_id`, so there's no cross-source dedupe. Also `resume_versions` and a `meta` KV (resume text, profile, answer pack). Migrations are ad-hoc `ALTER TABLE`.
7. **Score** (`scoring.py`): MiniLM cosine (resume↔JD) plus keyword coverage from a curated vocabulary. `fit = 0.5·cos + 0.5·coverage`, computed at read time. No tier, location or YOE term, and no "why".
8. **Tailor** (`tailor.py`, `llm.py`): one LLM call rewrites the whole plain-text resume. `grounding.py` then runs 4 deterministic checks against the original text, a human approves, and a **plain-text reportlab PDF** is generated (the LaTeX layout is lost).
9. **Apply**: a copy-paste "apply pack" plus an email drafter (`mailto:`). No browser automation. Statuses: new / interested / ready / applied / rejected / not_interested.
10. **Funding** (`funding.py`): RSS → LLM → `discovered_companies.yaml`; never run. **Schedule**: a launchd example that curls `/api/ingest` every 6 h; not installed.
11. **Tests**: 27 offline tests (parsers + grounding). None cover scoring or tailoring. No CI.

## 2. What I found

| # | Finding | Impact | Fix |
|---|---|---|---|
| F1 | DB resume text is still the leaked pytest placeholder (`test_resume.pdf`, 43 chars) | every stored match score is meaningless | P0 |
| F2 | 0 / 301 stored jobs carry compensation (Ashby should) | salary filter never fires | P1 |
| F3 | Undefined `raw` → `NameError` at `tailor.py:211` and `email_draft.py:113` | crash on an empty model reply | P0 |
| F4 | `/approve` accepts any text; the grounding gate exists **only in the browser** | the no-fabrication gate can be bypassed | P0 |
| F5 | `/draft-email` monkeypatches `store.get_job` globally | not thread-safe | P0 |
| F6 | store `db_path` default binds at def-time | already leaked test data once | P0 |
| F7 | 549-line `app.py`, no routers; deprecated `on_event` | every phase adds endpoints | P0 |
| F8 | reportlab paragraphs don't escape `<` / `>` | PDF crash on some text | P0 (then replaced in P3) |
| F9 | `min_score` filters `match_score`, not `fit_score`; "Hide senior+" defaults on | surprising filtering | P0 |
| F10 | YOE takes the *strictest* floor (Affirm "1.5+" parsed as 5) | good roles hidden | P2 |
| F11 | "Remote" outside India isn't checked for India eligibility | "Remote – US only" passes | P2 |
| F12 | `funding.py` computes a recency cutoff but never uses it | stale rounds qualify | P1 |
| F13 | 10 companies failed on wrong ATS guesses; 6 of them are now resolved | lost coverage | P1 |

- **`~/Downloads/jobs_testing/`** is the pre-commit copy of the grounding patch. It's **already merged** as `cb54d88`, minus the `drove` verb family, which was dropped on purpose. Nothing to apply.
- `files/Job automation/` is an empty Xcode template and is ignored.
- **Environment**:
  - Python 3.13 venv, with `playwright` now installed.
  - Node 22 available.
  - Google Chrome installed.
  - **No TeX engine.**
  - Ollama installed but not running.
  - No `.env`.

## 3. Design decisions (recommendations; override any)

- **D1. Frontend: Vite + React + TypeScript + Tailwind.** It builds into `server/static/dist` and FastAPI serves it, so Node isn't needed at runtime; `make dev` runs Vite with an API proxy.
  - **Why not HTMX:** the review card is the product. It needs:
    - JD and resume side by side, with a diff
    - inline editing of bullets, cover letter and answers, with live grounding flags
    - a kanban tracker
    - live status of a browser-fill session ("paused: CAPTCHA, your turn")

    That much client-side state would turn into server round-trips plus hand-written JS under HTMX. React keeps it in typed components, and Tailwind lets the editorial look be a small set of design tokens rather than a growing stylesheet.
  - **Cost:** a `web/` build step, kept out of the Python path.
- **D2. Scheduler: launchd, not APScheduler.**
  - A `StartCalendarInterval` LaunchAgent runs `python -m jobcopilot.daily` even when the server isn't open. If the Mac slept through the slot, it fires on wake.
  - **Default start is 19:00 IST**, so tailoring and fill-planning (20–40 min on a local model) are done when you open the app at 20:00. Configurable in `config/schedule.yaml`.
  - The app shows the run log and has a "Run now" button.
  - A second 2-hourly interval polls only short RSS feeds (Jobspresso).
- **D3. "Pre-filled" means a fill plan at night and the live fill on click.**
  - ATS forms are anonymous and don't keep drafts, and tabs left open overnight lose their session or CSRF state.
  - **Nightly:** build a **fill plan** (field → value, source, confidence, plus a list of unresolved fields). Data comes from public question APIs where they exist (Greenhouse `?questions=true`), otherwise from a read-only load of the public form. The resume and cover letter are made ready.
  - **On click:** **Open & Submit** launches your headed, persistent-profile Chrome, executes the plan in seconds, and stops on the final page.
- **D4. Tailoring works on structured facts.**
  - `profile/resume_facts.yaml` gives every bullet, skill and metric an ID.
  - The LLM returns a JSON **edit plan**, never free text: pick/reorder bullets by ID, rephrase a bullet, reorder skills.
  - Locked fields can't change: employer, title, dates, degree, numbers (L#2).
  - Jinja2 renders the plan into the chosen base `.tex`, then it's compiled to PDF.
  - Grounding becomes structural: each output line cites fact IDs, and the checks run per line against those facts.
  - **Fail-closed:** a failing line sends the job to `needs_review` instead of letting it through.
  - Uses ~5× fewer tokens than whole-resume rewrites.
- **D5. LaTeX: pdfLaTeX via TinyTeX.** Both base PDFs were made with **pdfTeX** in Times (NimbusRomNo9L). TinyTeX is a ~100 MB user-space install with no sudo, and it reproduces them exactly, including the pdfTeX `\pdfgentounicode` mapping that keeps the text ATS-extractable. Tectonic (XeTeX) was the alternative, but it lacks that mapping. A test enforces ATS-safety: no `ﬁ`/`ﬂ` ligatures or mojibake in extracted text, 1 page, round-trip parse ≥ 90 %.
- **D6. Privacy.**
  - `profile/` and `data/` are git-ignored; `profile.example/` with fake data is committed.
  - The browser profile lives at `~/.jobcopilot/browser-profile`, outside the repo.
  - A pre-commit/CI check fails on phone or email patterns in tracked files.
- **D7. Honest HTTP.** A single `jobcopilot/http.py` serves every connector:
  - honest UA: `job-copilot/2.0 (personal, non-commercial)`
  - per-host token-bucket rate limits and a robots.txt cache
  - `Retry-After` and backoff, ETag caching
  - fixture-record mode

  A failing source logs an error and never aborts the run.
- **D8. Nothing is dropped silently.** Every hard-filter exclusion (YOE, geo, salary, tier, body shop) is stored with its reason and a quoted excerpt (L#7), and shown in an **Excluded** view.
- **D9. OSS study pulled forward.** Its ideas are slotted into the phases they improve. Phase 6 becomes "remaining ideas + docs".
- **D10. YC: no automation on workatastartup.com or ycombinator.com.** YC's terms ban robots and scraping "including all subdomains".
  - YC companies are discovered through the community `yc-oss` dataset, using names, websites and batch only. Their jobs come from **their own ATS boards**.
  - `yc-oss` spot-checked 2026-11-11: 1,502 hiring companies, refreshed daily by a GitHub Action, and the batches match known companies (RevenueCat S18, AiPrise S22, LiteLLM W23, Emergent S24).
  - Each YC card offers **Apply on <ATS>**, which can be pre-filled, and **Apply on WaaS**, which is a link-out plus a drafted founder note you copy.
  - The YC login in the dedicated browser profile is never used by automation.
- **D11. Board rules.**
  - Every card shows a followable "via <source>" link.
  - Himalayas jobs are applied to through its `applicationLink`, and expired jobs are purged.
  - RSS sources store minimal fields plus the link.
  - The employer's own ATS URL is preferred only when we found the job there independently.
- **D12. Bangalore Startup Map: refreshed once a day in the 19:00 run** (your call, 2026-11-11; robots.txt allows it and the site has no terms page).
  - One conditional GET of the homepage payload: companies, stage, sector, website, hiring map.
  - Company pages are fetched **only for new or changed hiring companies**, at ≤ 1 request per 5 s, with an ETag cache.
  - `/api/` is never called, and their role feed is never mirrored; jobs come from the ATS APIs.
  - Attribution goes in the UI and README.
- **D13. Docker + AWS ECS** (your call, 2026-11-11).
  - `Dockerfile` + `compose.yaml` from Phase 0; CI builds the image.
  - Phase 3b deploys a **demo instance with fixture data only** to AWS ECS Fargate. Your profile, facts, DB and browser sessions never leave the Mac (non-negotiable 4), and autofill needs your local logged-in browser anyway.
  - Cost ≈ US$9/month if left running, ~$0 when stopped. I confirm with you before the first deploy.
  - Until the deployment exists, the matching résumé facts stay `pending` and tailoring won't use them.
- **D14. LLM.**
  - Groq models in order: `openai/gpt-oss-120b` → `qwen/qwen3.8-27b` → `openai/gpt-oss-20b`, with automatic fallback to local Ollama.
  - Groq is a cloud service, so prompts (facts + JD) leave the Mac when it's used. Ollama-only is one env var away.

## 4. Phases

☐ todo · ◐ in progress · ☑ done. Every phase ends the same way: tests green → app runs → commit on `v2` → this file updated → a ≤10-line summary to you.

### Phase 0: Foundations & fixes ☑
- ☐ Branch `v2`.
- ☐ `Makefile` with `setup`, `dev`, `test`, `daily`, `schedule-install` / `schedule-uninstall`, `smoke-live`.
- ☐ `requirements.txt` + `requirements-dev.txt`.
- ☐ GitHub Actions CI running pytest, the PII check and a Docker build.
- ☐ `Dockerfile` + `.dockerignore` + `compose.yaml` (D13).
- ☐ `llm.py` fixes:
  - provider fallback chain (Groq → Ollama)
  - current Groq models (the Llama 3.x defaults are retired)
  - reasoning models: `gpt-oss` needs a token floor and `reasoning_effort`; empty content with `finish_reason=length` becomes a clear error, not a silent empty reply
- ☐ `config/` directory:
  - `companies.yaml` moves here (the old path is still read)
  - new `filters.yaml`: currency floors, YOE range, role families, geo rules, tier thresholds, daily target
  - new `sources.yaml` and `schedule.yaml`
- ☐ `jobcopilot/http.py` (D7), with tests: rate limit honoured, robots disallow respected, 429 backoff.
- ☐ Versioned migrations (`schema_version` table). The store resolves `db_path` at call time (F6).
- ☐ Split `app.py` into routers and switch to `lifespan` (F7).
- ☐ Fix F3, F5, F8, F9.
- ☐ **Server-side grounding gate on `/approve`** (F4): re-check, then require an explicit confirmation of every flag.
- ☐ Clear the placeholder resume (F1).
- ☐ `profile/` scaffold + `profile.example/`:
  - copy the two base PDFs into `profile/base_resumes/`
  - **pre-fill `resume_facts.yaml` from those two PDFs only**, with every line `status: unconfirmed` (or `pending` for claims that aren't true yet)
  - `python -m jobcopilot.facts check` validates the file
  - I send you the lines to confirm while P1–P2 are built
- **Accept when**:
  - the old tests plus new tests are green, locally and in CI
  - `make dev` serves the app
  - `/approve` rejects ungrounded text (test)
  - no personal data appears in `git ls-files`
  - the facts file validates

### Phase 1: New sources ◐ (built; review pending)
- ☐ Connectors, each with a module, fixture, parse test and rate limit:

  | Connector | Endpoint / note |
  |---|---|
  | Himalayas | filtered `search` API |
  | Working Nomads | `exposed_jobs` |
  | Jobspresso | RSS, polled every 2 h |
  | RemoteOK | `/api` |
  | We Work Remotely | programming RSS |
  | SmartRecruiters | needed for Freshworks |
  | Gem | needed for PromptQL |
  | Greenhouse EU | Groww; investigate the API host |

  Every connector emits posting age and the time-zone / region requirement.
- ☐ `jobcopilot/ats_detect.py`, using the signals in `docs/SOURCES.md`:
  - redirects → URL/HTML regexes → iframe JS bundle → Phenom `applyUrl` → one detail page
  - always confirmed with one API call; the result is cached on the company record
- ☐ `config/target_companies.yaml`, pre-seeded with the detected boards:
  - LiteLLM (Ashby `litellm`), AiPrise (Ashby `aiprise`), Emergent (Greenhouse `emergentlabsinc`), Supabase (Ashby `supabase`)
  - OpenZeppelin (Greenhouse `openzeppelin`), Circle (Workday `circle`/wd1/`Circle`), RevenueCat (Ashby `revenuecat`)
- ☐ Fix the old failures:
  - Plaid → Ashby; Razorpay → Greenhouse; DigitalOcean → `digitalocean98`; Vercel → Greenhouse; Freshworks → SmartRecruiters; Hasura → Gem `promptql`
  - Groww pending (Greenhouse EU API host); Setu and HashiCorp → manual
- ☐ YC: `yc-oss` hiring list → company websites → detector (D10).
- ☐ Bangalore Startup Map seed import (D12).
- ☐ `companies` table: name, normalised name, domain, careers URL, ATS + id, **tier** (brand / funded / unknown / excluded), stage, investors, YC batch / top_company, signal sources, last checked.
  - Signals come from `config/brands.yaml`, yc-oss, BSM stage, and `funding.py` with the recency fix (F12).
- ☐ Cross-source dedupe (L#6): exact normalised URL → hash of normalised company + title + location → fuzzy title + company. A `job_sources` table keeps every source; apply-link rules per D11.
- ☐ Investigate F2 (compensation missing).
- **Accept when**:
  - every new connector and the detector pass offline fixture tests
  - the 7 targets resolve
  - a job seen on Himalayas and on Ashby becomes 1 row with 2 sources (test)
  - a failing source doesn't abort the run (test)
  - `make smoke-live` prints per-source counts

### Phase 1b: Many more sources (your request, 2026-11-11) ☐
- ☐ Survey the top-starred job-automation / job-aggregation repos on GitHub (e.g. JobSpy, AIHawk, ApplyPilot, jobpilot, and the topic lists `job-scraper`, `job-search`, `job-board`, `job-application-automation`) and extract **every job source they use**.
- ☐ For each candidate, apply the same gate as Phase 1 (official API / RSS / public JSON, robots.txt, terms) and record the verdict in `docs/SOURCES.md`. Sources that need scraping a ToS-protected site (LinkedIn, Indeed, Naukri, Glassdoor, Wellfound...) stay link-out only, even when popular repos scrape them.
- ☐ Candidates to check first:
  - remote and global boards: Remotive, Arbeitnow, Jobicy, The Muse, Findwork, Landing.jobs, 4dayweek, Web3.career / crypto boards
  - India: Hasjob, Instahyre, Cutshort, YC-style India startup lists
  - community lists: Hacker News "Who is hiring" (official HN / Algolia API); GitHub new-grad and early-career lists (SimplifyJobs, etc.) — strong for 0–2 YOE
  - more employer ATS boards: public lists of Greenhouse / Lever / Ashby / Workday employers, fed through `ats_detect.py`
- ☐ One connector per approved source: same contract as Phase 1 (fixture + offline test + rate limit + attribution), registered in `connectors/__init__.py` and `config/sources.yaml`.
- **Accept when**:
  - `docs/SOURCES.md` lists every surveyed source with a verdict and reason
  - every approved source has a passing offline test
  - a live ingest shows per-source counts

### Phase 2: Filtering, scoring, queue, nightly run ☐
- ☐ YOE v2:
  - anchor to the requirements section; handle `1.5+`, `0–2`, fresher / new grad / entry level
  - prefer the lowest hard floor on junior titles (F10)
  - store the evidence quote
  - LLM fallback only when the regex finds nothing or is ambiguous
- ☐ Hard filters from `config/filters.yaml`, each exclusion stored with its reason (D8):
  - YOE: drop ≥ 3, configurable
  - **Geo**: outside India must be remote **and** India-eligible (F11). Uses Himalayas `locationRestrictions` / `timezoneRestrictions`, the WWR `region` field, a free-text parser ("US only", "EU only", "Americas", "APAC", "Worldwide", "India"), and IST working-hours overlap.
  - Salary floors in INR / USD / EUR; roles with no salary are kept but ranked lower.
  - Company tier must be brand or funded; body-shop, staffing-agency and commission-only denylist (L#7).
- ☐ Fit score 0–100 with visible weights (L#7):
  - skills from your facts vs the JD (must-have > nice-to-have; whole-term matching; synonyms like Spring Boot / springboot, GCP / Google Cloud, Drools / DRL)
  - plus role, seniority/YOE, location, tier, salary signal, freshness, and semantic similarity as one component
  - stays LLM-free
  - returns the *why*: matched skills, missing skills, a bar per component
- ☐ Daily queue: target 15–25, ≤ 3 per company, never padded. Close misses go in a separate "near-miss" lane.
- ☐ `python -m jobcopilot.daily` + LaunchAgents (D2) + a `runs` table with per-source health + "Run now".
- **Accept when**:
  - YOE tests on ≥ 30 real JD snippets, including the Affirm miss
  - geo tests: "Remote – US only" excluded, "Remote – APAC" kept, time-zone overlap
  - INR / USD / EUR salary tests
  - a scoring-explanation test
  - `make daily` on fixtures produces a ranked queue
  - the agent installs and runs via `launchctl kickstart`

### Phase 3: Tailoring v2 ☐
- ☐ Base templates:
  - `profile/base_resumes/backend_sde.tex` from `AdityaBarnwal_.pdf`, `ml_ai.tex` from `AdityaBarnwal_DS.pdf`, both pdfLaTeX (D5) with the same layout, fonts and section order
  - base picked by role family
  - a new base is added by dropping in a file whose header says `% roles: …`
- ☐ Edit-plan tailoring (D4):
  - show missing keywords *before* tailoring so you can strike them
  - a keyword goes in only if a fact backs it or you've **attested** it
  - novel-number check
  - LLM-leak phrases rejected; each retry starts a fresh conversation (L#2)
  - one-page guard using the real PDF page count
- ☐ Cover letter only when the fill plan shows a cover-letter field or upload, or the role is email-apply:
  - ≤ 180 words
  - cliché denylist
  - fact-drift check: no new numbers or proper nouns (L#2)
- ☐ `profile/answers.yaml`, a typed answers bank: notice period, expected CTC, work authorisation, relocation, "why this company" template, …
  - question matcher plus grounded LLM adaptation for open questions
  - numbers only ever come from the YAML
  - a **Needs you** queue saves each new answer back to the bank (L#4)
- ☐ Grounding v3: per-line fact citations + the 4 existing checks, enforced server-side. Tailoring refuses to run if any cited fact is unconfirmed.
- ☐ Versioned artifacts: `data/applications/<job>/v<N>/{resume.tex,resume.pdf,cover_letter.*,answers.json,edit_plan.json}`.
- ☐ **3b. AWS ECS demo deploy (D13)**:
  - ECR image + ECS Fargate service in demo mode (fixture DB, no profile)
  - `make deploy-demo` / `make stop-demo`, plus a README section
  - needs your AWS account + `aws configure`; the first deploy happens only after your OK
  - then flip the matching résumé facts from `pending` to `confirmed`
- **Accept when**:
  - golden renders of both bases are 1 page and pass the ATS round-trip
  - fabricated skill / metric / title / employer are each flagged, while a legitimate rephrase passes
  - cover-letter tests pass: length, clichés, drift
  - answer-matcher tests pass
  - the demo is reachable on ECS and stops with one command

### Phase 4: Form autofill (stops before Submit) ☐
- ☐ Playwright, headed Chrome, persistent profile `~/.jobcopilot/browser-profile` (created this session).
  - A "Sign in to portals" button opens it for you to log in manually.
  - No passwords stored or typed; no accounts created.
- ☐ **Safety layer, shared by every adapter**:
  - `SafePage` exposes only fill / select / upload / check / next-step.
  - Any click on a Submit / Apply / Send / Finish-like element raises.
  - A per-adapter network guard aborts known final-submit endpoints.
  - The run finishes with a `ready | blocked | needs_you` outcome and a submit selector that matches **exactly one** element; code highlights it and **never clicks it** (L#1).
  - CAPTCHA, Turnstile or a login wall → `needs_you` plus a UI notification; challenge frames are never touched.
  - Page text is treated as **untrusted** in any LLM prompt (prompt-injection defence).
- ☐ Declarative adapter files per ATS (L#3):
  - **Greenhouse**, **Lever**, **Ashby**
  - **Workday**: My Information → Experience → Questions → Voluntary Disclosures → Review; the `beecatcher` honeypot is never filled; a sign-in or account wall pauses for you
  - **Generic**: label / aria / name / placeholder matching + LLM field mapping with confidence; low-confidence fields are left empty and highlighted
- ☐ Before touching any form: check location eligibility and re-check the posting is still live (≤ 6 h) (L#8).
- ☐ Fill plans built nightly (D3) and executed on click; status → "Ready for review".
- ☐ Denylist, link-out only: LinkedIn, Naukri, Indeed, Wellfound, workatastartup.com, ycombinator.com, and every DO-NOT-AUTOMATE row in `docs/SOURCES.md`.
- **Accept when**:
  - per-ATS tests against locally served fixture forms: known fields filled, uncertain ones empty and highlighted, file attached
  - **a test proving Submit is never clicked or POSTed even when the page invites it**
  - a CAPTCHA fixture → `needs_you`; a denylisted URL → link-out
  - one supervised live dry run per ATS

### Phase 5: Review UI ☐
- ☐ Stack per D1. Editorial design: serif display face plus a clean sans body, generous whitespace, one accent colour, light and dark themes.
- ☐ Screens:
  - **Queue**: score, why, YOE, location, source + attribution, age, time zone
  - **Review card**: JD next to the resume diff vs base, cover letter, answers; inline edit; grounding flags
  - **Open & Submit**: launch the fill → you press Submit → the confirmation page is detected and a screenshot saved → "Mark submitted" (L#8)
  - **Tracker**: Found → Tailored → Filled → Submitted → OA → Interview → Offer / Rejected, with dates, follow-up reminders and re-apply cooldowns (L#6)
  - also: **Needs you** · **Facts & answers** (confirm lines) · **Excluded** · **Runs**
- ☐ Migrate v1 statuses. Guard the CSV export against formula injection.
- **Accept when**:
  - a Playwright e2e test passes: queue → review → open → mark submitted, on a fixture DB
  - every screen is keyboard-navigable
  - screenshots are captured for the README

### Phase 6: Learnings, docs, done ☐
- ☐ Refresh `docs/LEARNINGS.md` and implement the remaining top ideas.
- ☐ `README.md`: setup, a Mermaid architecture diagram, screenshots, safety guarantees, source attributions.
- **Accept when**: the Definition of Done (brief §6) is checked line by line.

## 5. Non-negotiables and where each is enforced
1. **Never auto-submit.** `SafePage` + submit-guard tests (P4); no submit code path exists.
2. **Never fabricate.** Facts-only edit plans, locked fields, server-side grounding gate (P0/P3).
3. **Never bypass CAPTCHAs or bot checks; never automate prohibited sites.** `needs_you` pause; domain denylist; `docs/SOURCES.md`; no stealth flags.
4. **Never store passwords or card/ID numbers; data stays local.** Git-ignored `profile/` + `data/`; PII check in CI; manual logins only.
5. **Respect rate limits and robots.txt; prefer official APIs.** `http.py` (P0) + per-source verdicts.

## 6. Your decisions (2026-11-11)
1. **Salary floors**: INR 15 LPA kept. You didn't give USD / EUR floors, so defaults are **USD 30,000 / EUR 28,000 per year**, editable in `config/filters.yaml`.
2. **YC**: use `yc-oss` (verified, see D10).
3. **Installs**: approved (TinyTeX, Node packages, Python deps).
4. **LLM**: Groq key copied from the jarvis project into this repo's git-ignored `.env` (D14).
5. **Nightly run**: 19:00 IST.
6. **Bangalore Startup Map**: daily refresh in the 19:00 run (D12).
7. **Docker / AWS ECS / "verifies every claim"**: make them real (D13, Phases 3 / 3b).

Still open, not blocking until Phase 3b: is your AWS account ready?

## 7. Progress log
- 2026-11-11: Repo mapped; sources, ATS map, OSS repos and prior docs researched; plan written.
- 2026-11-11: Answers received (§6); Phase 0 started on branch `v2`.
- 2026-11-11: **Phase 0 built and committed (paused for usage limits).** 170 tests green, PII check clean, app runs on the real DB (migrated to schema v1, backup in `data/backups/`), live ingest through the polite client: 4,307 raw → 328 passed, 292 new.
  - **Resume here (Phase 0 wrap-up):**
    1. Re-run the 3-lens review (`jobcopilot-phase0-review` workflow; it was stopped before it finished) and fix whatever it confirms.
    2. You confirm résumé facts: `.venv/bin/python -m jobcopilot.facts review`, then tell me "confirm all except …".
    3. Run `sudo xcodebuild -license accept` once, so plain `make` works (until then use `/Library/Developer/CommandLineTools/usr/bin/make`).
  - **Backlog found while building:**
    - sort jobs by `fit_score`, not `match_score` (P2)
    - `max_yoe` is applied after the row limit (P2)
    - PDF filename can collide when two approvals land in the same second (P3)
    - the email modal's copy button has only the browser-side grounding check (P5)
    - Postman's Greenhouse board now returns 404 (P1, ATS detection)
    - `funding.py` recency fix F12 (P1)
  - Then start Phase 1.
- 2026-11-11: **Phase 0 done.** A 3-lens review found 31 issues (4 high); every one was reproduced with a failing test, then fixed.
  - **Highlights:**
    - grounding now catches base-form verbs, title inflation and spelled-out numbers
    - the reuse path is re-grounded server-side
    - an RFC 9309 robots parser replaces the stdlib one (it failed open on BOM / duplicate groups)
    - DO-NOT-AUTOMATE host denylist inside the HTTP client
    - credential headers are dropped on cross-origin redirects
    - `usable_facts()` fails closed
    - strict CSP + http(s)-only links in the UI
    - the PII guard covers documents, databases, name and links, bare mobiles and API keys
    - per-model fallback within Groq
  - **Verification:** 402 tests green, PII clean, live ingest OK (4,318 raw → 330 passed), real DB migrated to schema v2.
  - **Backlog added:**
    - `tailor.suggest` / `apply-pack` should re-ground stored versions too (P3)
    - facts unknown-key check and `absent_in:` (P3)
    - `/api/ingest` response should list robots-blocked companies (P5)
- 2026-11-11: **Phase 1 built** (4 builders), integrated and run live.
  - **Live ingest:** 37 company boards + 5 job boards → 770 jobs stored, 124 of them from boards; 47 with real salary numbers (F2 fixed: Ashby nests salary in `summaryComponents`).
  - **Discovery:** 2,511 companies with tiers (brand 174 · funded 1,894 · unknown 431 · excluded 12).
  - **Targets:** all 7 resolved; PromptQL via Gem (robots exemption, your call). Freshworks stays manual (SmartRecruiters robots disallow). Postman's board still 404s.
  - **Himalayas:** robots.txt forbids paging, so page 1 per query only.
  - **Tests:** 537 green, PII clean.
  - **Next:** Phase 1 review → fixes → mark ☑ → Phase 1b (many more sources).
