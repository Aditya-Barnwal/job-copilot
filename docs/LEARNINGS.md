# Learnings from open-source job-application projects

Before building v2, several open-source job-application tools were studied
(READMEs and key modules, read through the GitHub API) to borrow what works
and to avoid what gets accounts banned or applications fabricated. This page
lists each idea and what v2 actually did with it.

The licence decides what may be reused. Code from MIT or Apache-2.0 repos may
be adapted with attribution (for Apache: keep LICENSE / NOTICE and mark
changes). AGPL, unlicensed and non-commercial repos are **ideas only**: no
code is copied from them.

| Repo | Licence | Reuse |
|---|---|---|
| Pickle-Pixel/ApplyPilot | AGPL-3.0 | ideas only |
| feder-cr/Jobs_Applier_AI_Agent (AIHawk) | MIT | code may be adapted |
| shwechau/applymatic | none (all rights reserved) | ideas only |
| speedyapply/JobSpy | MIT | code may be adapted |
| srbhr/Resume-Matcher | Apache-2.0 | code may be adapted (NOTICE) |
| suxrobGM/jobpilot | MIT | code may be adapted |
| Liam-Frost/AutoApply | PolyForm-Noncommercial | ideas only |

Status key: **Adopted** · **Partly adopted** (what differs and why) · **Not
adopted** (why).

## Ideas, by project

### ApplyPilot (ideas only)

| Idea | Status | In v2 |
|---|---|---|
| Layered page extraction: JSON-LD `JobPosting` → intercepted API JSON → CSS → LLM, with the LLM choosing a strategy from a page summary | **Not adopted** | v2 never scrapes job pages. It ingests only official board APIs and feeds, which are already structured, so there is nothing to extract. |
| Tailoring returns JSON; code assembles the document; each retry is a fresh conversation | **Adopted** | The edit plan over fact ids (`tailor_v2.py`); the LaTeX document is rendered in code. |
| Validator: preserved companies and schools, real-metrics check, fabrication watch-list, LLM leak phrases | **Adopted** | Locked headers, verbatim metrics, per-line grounding (`grounding.py`), leak-phrase rejection in tailoring and writing. |
| Result codes (expired, CAPTCHA, not eligible); location eligibility checked before touching a form; lists of SSO-only and manual ATSs | **Adopted** | Fill outcomes `ready / needs_you / blocked / error`. Location eligibility is decided at ingest, before a job can reach the queue or a nightly fill plan, and a fill refuses postings that are gone or already submitted. The do-not-automate list and `manual` ATSs are link-out only. |

### AIHawk (MIT)

| Idea | Status | In v2 |
|---|---|---|
| One declarative file per ATS: hosts, selectors, honeypots, error patterns, confirmation URL | **Adopted** | `autofill/adapters/specs/*.yaml` for Greenhouse, Lever, Ashby and Workday; Workday's `beecatcher` honeypot is never filled. |
| Filler ends with `{status, summary, submit_selector}`; a selector counts only if it matches exactly one element; a loop watchdog; page text treated as untrusted LLM input | **Adopted** | `executor.py` returns an `Outcome`, highlights the single Submit and never clicks it, and caps the number of steps. Page text and JDs are fenced as untrusted data in prompts. |
| After the human submits, confirm from URL or text and keep a screenshot; guard CSV exports against formula injection | **Adopted** | Passive confirmation watch after handover with a proof screenshot; the tracker CSV export escapes formula-like cells. |
| Drop postings that only link to LinkedIn; prefer the employer's own form over a board copy | **Partly adopted** | The employer's ATS link is preferred when the job was found there independently, and board terms decide otherwise (`apply_rule`). Postings whose apply link is on a do-not-automate host aren't dropped: they become link-out only, so a good role isn't lost. |

### applymatic (ideas only)

| Idea | Status | In v2 |
|---|---|---|
| Re-apply cooldowns that depend on the outcome | **Partly adopted** | One cooldown after a rejection (`queue.reapply_cooldown_days`); per-outcome cooldowns weren't needed yet. |
| Show missing keywords *before* tailoring and let the user strike them | **Adopted** | The keyword preview runs before any LLM call. |
| `detect_ats(url)` routing; statuses filled → awaiting review → submitted; visible browser, human clicks Submit | **Adopted** | ATS detection for discovery and for choosing a fill adapter; tracker statuses; a headed browser and a human Submit. |

### JobSpy (MIT; schema only, its scrapers are not used)

| Idea | Status | In v2 |
|---|---|---|
| A `JobPost` shape with direct URL, pay interval and range, currency, remote flag and experience range; annualisation of hourly, daily, weekly and monthly pay | **Adopted** | The `Job` and `Compensation` models; pay is annualised before it's compared with a floor. |
| A failing source records an error and never aborts the run | **Adopted** | Per-source status in ingest and the `runs` table. |

### Resume-Matcher (Apache-2.0)

| Idea | Status | In v2 |
|---|---|---|
| Edits limited to allowed paths; company, title, dates and degree locked; verify the original text before applying a diff | **Adopted** | Only bullets may be rephrased; approval re-checks the plan against the *current* facts and refuses if the cited facts changed. |
| A novel-numbers check that normalises units and currency and ignores years and versions; review required below a similarity floor | **Adopted** | Grounding's new-number and low-similarity checks. |
| A missing keyword may be inserted only if the user marks it attested | **Adopted** | Attested keywords in the keyword preview. |
| The LLM only scores bullets and selection is deterministic; real PDF page count; a PDF round-trip parse | **Partly adopted** | The real page count and an ATS round-trip check are used. Selection is done by the LLM's edit plan instead, because choosing which facts answer a JD is the useful part; every choice is still a fact id and is grounded line by line. |
| Whole-term keyword matching (`java` is not `javascript`) | **Adopted** | `skills.py`, used by the fit score and the keyword preview. |

### jobpilot (MIT)

| Idea | Status | In v2 |
|---|---|---|
| Duplicate check: exact normalised URL, else fuzzy title and company | **Adopted** | Cross-source dedupe by normalised URL, then by a normalised company, title and location key; submitted jobs never reappear in the queue. |
| A "needs you" queue with typed requests and a saved-answer key that writes back to the answers bank | **Adopted** | Unanswered form questions are collected from fill plans, and the UI saves an answer straight back to the bank. |
| A licence check before adding any new source | **Adopted** | The source gate in [`SOURCES.md`](SOURCES.md). |

### AutoApply (ideas only)

| Idea | Status | In v2 |
|---|---|---|
| Hard rules by regex, keeping the quoted excerpt as evidence (experience, sponsorship, degree, staffing-agency and commission-only spam) | **Adopted** | Every eligibility reason carries an excerpt; agencies and commission-only employers are an excluded tier. |
| Visible score weights | **Adopted** | The fit score's components and weights are in `config/scoring.yaml` and shown per job. |
| A content hash over stable fields; freshness limits before generating and before hand-off | **Partly adopted** | Hand-off requires a fresh posting and plan (`freshness_hours`, `plan_stale_hours`). Content hashes are used to skip unchanged sources and datasets rather than per job, since dedupe keys already identify a job. |
| Fact-drift check: numbers and proper nouns not in the evidence, JD or profile | **Adopted** | Grounding in cover letters, emails and open answers. |

## Anti-patterns seen, and excluded here

- Autonomous submit, including "submit after one batch confirmation".
- CAPTCHA solvers, or clicking checkbox CAPTCHAs.
- Stealth browsers and flags such as `--disable-blink-features=AutomationControlled`.
- Auto-created accounts or stored portal passwords (one project hard-codes a
  Workday password); passwords inside LLM prompts.
- Reading email for verification codes.
- LinkedIn or Indeed private-API scraping, proxy rotation, TLS impersonation.
- "Answer YES for nearby tools" or "pick the most plausible number" when data
  is missing.
- Inserting skills the user didn't confirm.

Each of these is excluded by design; [`DESIGN.md`](DESIGN.md#17-non-negotiables-and-where-each-is-enforced)
shows where.

## The ranked top ideas, and where they landed

| # | Idea | Status | Where |
|---|---|---|---|
| 1 | Filler outcome `ready / blocked / needs_you` with a single-match Submit that code highlights but never clicks; missing data blocks, never guessed | **Adopted** | `autofill/executor.py`, `safepage.py`, `guard.py` |
| 2 | Locked fields, allowed-path edits, novel-number check, attested keywords, cover-letter fact drift | **Adopted** | `tailor_v2.py`, `grounding.py`, `writing/` |
| 3 | Declarative per-ATS adapter files | **Adopted** | `autofill/adapters/specs/` |
| 4 | Typed answers bank plus a "needs you" queue that saves reusable answers | **Adopted** | `answers.py`, the answers screen in the UI |
| 5 | Official ATS JSON plus layered page extraction; the employer's apply URL preferred where the source's terms allow | **Partly adopted** | Official APIs and feeds only, no page extraction (see ApplyPilot above); `apply_rule` per board |
| 6 | Dedupe by URL, fuzzy title and company, content hash; re-apply cooldowns | **Adopted** (with the variations above) | `dedupe.py`, `queue.py` |
| 7 | Regex hard rules with quoted evidence before scoring; whole-term matching; visible weights | **Adopted** | `eligibility.py`, `skills.py`, `fit.py` |
| 8 | Freshness re-check before hand-off; confirmation-page detection and a screenshot after the human submits; CSV formula guard | **Adopted** | `autofill/service.py`, the tracker export |
