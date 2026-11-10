# Learnings from open-source job-application projects

Studied **2026-11-11** (README + key modules via the GitHub API). Licence decides what we may reuse:
code from MIT / Apache-2.0 repos may be adapted with attribution (Apache: keep LICENSE/NOTICE, mark changes);
AGPL / unlicensed / non-commercial repos are **ideas only — no code copied**.

| Repo | Licence | Reuse | ★ | Last push |
|---|---|---|---|---|
| Pickle-Pixel/ApplyPilot | AGPL-3.0 | ideas only | 1.8k | 2026-03 |
| feder-cr/Jobs_Applier_AI_Agent (AIHawk) | MIT | code OK | 32k | 2026-11 |
| shwechau/applymatic | none (all rights reserved) | ideas only | 0 | 2026-04 |
| speedyapply/JobSpy | MIT | code OK | 4.7k | 2026-10 |
| srbhr/Resume-Matcher | Apache-2.0 | code OK (NOTICE) | 29k | 2026-11 |
| suxrobGM/jobpilot | MIT | code OK | 112 | 2026-11 |
| Liam-Frost/AutoApply | PolyForm-Noncommercial | ideas only | 59 | 2026-08 |

## Ideas worth taking (→ phase)

**ApplyPilot** (ideas only)
- Layered extraction: JSON-LD `JobPosting` → intercepted API JSON → CSS → LLM last; the LLM picks a strategy from a page *summary*, not raw DOM. → P1
- Tailoring returns JSON; code assembles the document and header itself; each retry is a fresh conversation. → P3
- Validator: preserved companies/school, real-metrics check, fabrication watch-list, "LLM leak phrases" ("As an AI…"). → P3
- Result codes (`EXPIRED`, `CAPTCHA`, `FAILED:not_eligible_location`); check location eligibility *before* touching a form; `blocked_sso` / `manual_ats` lists. → P4

**AIHawk** (MIT)
- One declarative file per ATS (hosts, apply path, selectors, honeypots, error pattern, confirmation URL). Greenhouse: `#first_name`, hidden `#resume` uploaded via the labelled button, custom questions `#question_<n>` by label, comboboxes by role. Workday: `data-automation-id` (`legalNameSection_firstName`, `file-upload-input-ref`, `bottom-navigation-next-button`); **never type into the `beecatcher` honeypot**. → P4
- Filler finishes with `{status: ready|blocked, summary, submit_selector}`; a selector is accepted only if it matches **exactly one** element; loop watchdog; **page text is treated as untrusted input** to the LLM (prompt-injection defence). → P4
- After the human submits: read URL/text to confirm, keep a screenshot as proof. CSV export guarded against formula injection. → P5
- Drop postings that only link to LinkedIn; prefer the employer's own form over a board copy. → P1

**applymatic** (ideas only)
- Re-apply cooldowns (180 / 90 / 30 days: same role / other role / ATS rejection). → P5
- Show missing keywords *before* tailoring and let the user strike any. → P3
- `detect_ats(url)` routing; statuses `FORM_FILLED → AWAITING_REVIEW → SUBMITTED`; visible browser, human clicks Submit. → P4

**JobSpy** (MIT — schema only; its scrapers are not used)
- `JobPost` shape: `job_url_direct`, `interval/min_amount/max_amount/currency`, `is_remote`, `experience_range`; annualisation 2080/260/52/12. → P1
- A failing source records an error and never aborts the run. → P1

**Resume-Matcher** (Apache-2.0)
- Edits limited to allowed paths (summary, bullets, skills); company/title/dates/degree are **locked**; verify the original text before applying a diff. → P3
- `_novel_numbers()` normalises units/currency and ignores years/versions; grounding review required when similarity to source < 0.45. → P3
- A missing keyword can be inserted only if the user marks it **attested**. → P3
- LLM only *scores* bullets; selection is deterministic; real PDF page count; PDF round-trip parse must recover ≥ 90 % of fields. → P3
- Whole-term keyword matching (`java` ≠ `javascript`). → P2

**jobpilot** (MIT)
- Applied-duplicate check: exact normalised URL at any age, else fuzzy title+company within a window. → P1/P5
- "Needs you" queue (`question | choice | two_factor | approval`, expiry, saved-answer key → answers bank). → P3/P5
- Licence check before adding any new source. → P1

**AutoApply** (ideas only)
- Hard rules by regex with the **quoted excerpt** kept as evidence (experience, sponsorship, degree, staffing-agency / commission-only spam). → P2
- Visible score weights (skills 0.45 split must-have 70 / nice 30, keywords 0.35, rules 0.20). → P2
- `content_hash` over stable fields; freshness limits (re-check posting ≤ 24 h before generating, ≤ 6 h before hand-off). → P1/P4
- Fact-drift check: numbers and proper nouns not in evidence / JD / profile. → P3

## Anti-patterns seen — explicitly excluded here
Autonomous submit (incl. "submit after one batch yes") · CAPTCHA solvers / clicking checkbox CAPTCHAs · stealth browsers, `--disable-blink-features=AutomationControlled` · auto-created accounts or stored portal passwords (one hard-codes a Workday password) · passwords inside LLM prompts · reading email for verification codes · LinkedIn / Indeed private-API scraping, proxy rotation, TLS impersonation · "answer YES for nearby tools" or "pick the most plausible number" for missing data · inserting skills the user didn't confirm.

## Top ideas, ranked, and where they land
1. Filler outcome `ready | blocked | needs_you` + single-match submit selector that code **highlights but never clicks**; missing data ⇒ blocked, never guessed. → **P4**
2. Locked fields + allowed-path edits + novel-number check + attested keywords + cover-letter fact-drift. → **P3**
3. Declarative per-ATS adapter files (selectors, honeypots, errors, confirmation URLs). → **P4**
4. Typed answers bank + "Needs you" queue that saves reusable answers. → **P3/P5**
5. Official ATS JSON + layered extraction (JSON-LD → selectors → LLM); employer apply URL preferred where the source's terms allow. → **P1**
6. Dedupe: exact URL → fuzzy title+company → content hash; re-apply cooldowns. → **P1/P5**
7. Regex hard rules with quoted evidence before scoring; whole-term matching; visible weights. → **P2**
8. Freshness re-check before hand-off; confirmation-page detection + screenshot after the human submits; CSV formula guard. → **P4/P5**
