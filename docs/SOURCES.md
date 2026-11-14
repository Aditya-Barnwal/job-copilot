# Job sources — access rules

Checked **2026-11-11** with an honest UA (`job-copilot-research/0.1`), ≤ ~6 requests per host.
Re-check terms when a source starts failing or every ~6 months. Anything marked UNVERIFIED was not confirmed first-hand.

## Verdicts

| Source | Verdict | How we ingest | Must-dos |
|---|---|---|---|
| Greenhouse / Lever / Ashby / Workday CXS | OK-API (public board APIs) | existing connectors | per-host rate limit (plus a shared per-platform bucket) |
| SmartRecruiters | **robots disallow** (`User-agent: * Disallow: /`, only LinkedInBot allowed; checked 2026-11-11) | connector built, not used | Freshworks stays manual — do not exempt |
| Gem | OK-API — robots.txt unreadable (403), **exempted** as a documented public job-board API (user's call, 2026-11-11) | `api.gem.com/job_board/v0/{slug}/job_posts/` | rate-limited like every host |
| **Himalayas** | OK-API + **attribution** | `/jobs/api/search` (filtered, few pages/day) | visible "via Himalayas" link; **Apply must use `applicationLink`**; drop jobs after `expiryDate`; no bulk collection; free key → 60/min, 10k/day |
| **Working Nomads** | OK-API | `/api/exposed_jobs/` (≈36 recent, no paging) | no resale / competing product |
| **Jobspresso** | OK-RSS, personal use only | `/feed/job_feed/` (path form — `?feed=` is robots-disallowed) | feed holds only ~10 newest (≈1–2 h) → poll every 2 h; store minimal fields + link; no HTML scraping |
| **RemoteOK** | OK-API + **attribution** | `/api` (element 0 = legal notice) | followable link back + "Remote OK" named as source on the screen showing the data; Crawl-delay 1 s; no logo |
| **We Work Remotely** | OK-RSS + **attribution** | `/categories/remote-programming-jobs.rss` (+ others) | attribute links; RSS only — HTML is Cloudflare-challenged and API terms forbid storing (UNVERIFIED exact wording) → keep minimal fields + link |
| **YC Work at a Startup / ycombinator.com** | **DO-NOT-AUTOMATE** | none | ycombinator.com/legal ("including all subdomains"): *"you will not engage in or use any data mining, robots, scraping or similar data gathering or extraction methods"*. Link out only; human applies. |
| yc-oss API (`yc-oss.github.io/api`) | OK-API, unofficial | `companies/hiring.json` → company websites → ATS detector | built from YC's Algolia index by a GitHub Action; **no licence** → standing unclear; used only to discover company names/websites/batch, never jobs |
| **Bangalore Startup Map** | Attribution; owner consent for recurring use | one-off seed import from the public homepage payload (`self.__next_f` RSC data: 1,095 entries with stage, sector, website); ~129 company pages carry ATS role IDs (`greenhouse:<slug>:<id>`) | **never call `/api/`** (robots-disallowed); don't mirror their role feed — fetch jobs from ATS APIs; commercial site → ask owner before recurring automation |
| LinkedIn, Naukri, Indeed, Wellfound (and any ToS-prohibited site) | DO-NOT-AUTOMATE | none | link out only |

## robots.txt on the API hosts we call (checked 2026-11-11)

| Host | robots.txt | Effect |
|---|---|---|
| boards-api.greenhouse.io | 200 · `Disallow: /embed/` | `/v1/boards/…` allowed |
| api.lever.co | 200 · `Allow: /`, `Crawl-delay: 1` | allowed; delay honoured automatically |
| *.myworkdayjobs.com (e.g. paypal.wd1) | 200 · `Allow: /jobs/`, `Disallow: /refreshFacet/` | CXS `/wday/cxs/…` allowed |
| api.ashbyhq.com | **401** (unreadable) | exempted in `config/sources.yaml` — documented public Job Postings API |
| api.adzuna.com | 200 · `Disallow: /` | **not** exempted (key-gated, unused) — decide explicitly before adding keys |
| jsearch.p.rapidapi.com | 200 · `Disallow: /` | **not** exempted (key-gated, unused) |

`jobcopilot/http.py` enforces robots.txt for every other host (RFC 9309 longest-match), treats an unreadable robots.txt (401/403/5xx/network error) as "disallow" unless the host is exempted with a written reason, and honours `Crawl-delay`.

## Field coverage (what each source gives us)

| Source | Salary | Time-zone / region | Posted date | Description |
|---|---|---|---|---|
| Himalayas | `minSalary/maxSalary/currency/salaryPeriod` (often null) | `locationRestrictions[]`, `timezoneRestrictions[]` (UTC offsets) | `pubDate` epoch | HTML |
| Working Nomads | — | free-text `location` ("Europe, LATAM, APAC…") | ISO | HTML |
| Jobspresso | text (`job_listing:salary`) | free-text location | `pubDate` | `content:encoded` |
| RemoteOK | `salary_min/max` (0 = none) | free-text `location` (≈50 % filled) | epoch | HTML |
| We Work Remotely | free text in body | `region` ("USA Only", "Anywhere in the World") | `pubDate`, `expires_at` | HTML |
| Ashby | `compensationTierSummary` when `shouldDisplayCompensationOnJobPostings` | `isRemote`, location | `publishedAt` | HTML |
| Greenhouse | list endpoint: none (detail UNVERIFIED) | location text | `updated_at` | `content=true` |

## Target companies (detected 2026-11-11)

| Company | ATS | Board id | Live jobs | Pay shown | Notes |
|---|---|---|---|---|---|
| LiteLLM (BerriAI) | Ashby | `litellm` | 14 | yes | careers URL redirects to Ashby |
| AiPrise | Ashby | `aiprise` | 20 | yes | several Bengaluru SWE roles |
| Emergent | Greenhouse | `emergentlabsinc` | 51 | no | careers iframe → custom SPA; token found in its JS bundle; most roles Bengaluru |
| Supabase | Ashby | `supabase` | 75 | rarely | many "Remote, Global" / "Remote, APAC" |
| OpenZeppelin | Greenhouse | `openzeppelin` | 7 | no | own page with `gh_jid`; embed reveals slug |
| Circle | Workday (behind a Phenom front end) | tenant `circle`, `wd1`, site `Circle` | 61 | no | "Bangalore – remote first in India" facet |
| RevenueCat | Ashby | `revenuecat` | 10 | yes | all remote (Americas/APAC) |

## Previously failing companies — corrected

| Company | Was | Now |
|---|---|---|
| Plaid | greenhouse `plaid` (404) | **Ashby `plaid`** |
| Razorpay | lever (404) | **Greenhouse `razorpaysoftwareprivatelimited`** |
| Groww | lever (404) | **Greenhouse EU** board `groww` — EU API host unresolved (`boards-api.eu.greenhouse.io` NXDOMAIN) → investigate |
| Setu | lever (404) | TurboHire (`pinelabsgroup.turbohire.co`) — no connector; manual |
| DigitalOcean | greenhouse `digitalocean` (404) | **Greenhouse `digitalocean98`** |
| HashiCorp | greenhouse (404) | moved to IBM careers (UNVERIFIED) — manual |
| Vercel | ashby (0 roles) | **Greenhouse `vercel`** |
| ThoughtSpot | greenhouse (404) | Workday `thoughtspot`/`wd5`/`careers` — CXS returned 0 (UNVERIFIED) |
| Freshworks | greenhouse (404) | **SmartRecruiters `Freshworks`** |
| Hasura | greenhouse (404) | now PromptQL → **Gem `promptql`** |

## ATS detection signals (for `ats_detect.py`, in order)

1. Final URL after redirects (`jobs.ashbyhq.com/<slug>`, `jobs.lever.co/<slug>`, `*.myworkdayjobs.com`).
2. Regexes over href/src/raw HTML:
   - Ashby `jobs\.ashbyhq\.com/([\w.-]+)`
   - Greenhouse `(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_(?:board/js|app)\?for=)?([\w-]+)`; `[?&]gh_jid=\d+` ⇒ Greenhouse, slug from one detail page's `for=`
   - Workday `([\w-]+)\.wd(\d+)\.myworkdayjobs\.com/([\w-]+)` (also inside JSON `"applyUrl"`)
   - Lever `jobs\.lever\.co/([\w-]+)`, SmartRecruiters `careers\.smartrecruiters\.com/(\w+)`, Gem `jobs\.gem\.com/([\w-]+)`, TurboHire `([\w-]+)\.turbohire\.co`
   - Phenom (`cdn.phenompeople.com`, `"refNum"`) is a front end → read `applyUrl` on its search page to find the real ATS.
3. Follow `<iframe src>` and grep its JS for `boards-api.greenhouse.io` / `BOARD_TOKEN`.
4. Still nothing → follow one job-detail link; else mark `manual`.
5. Always confirm with one API call; on 429 back off, never loop.

## Phase 1b survey (2026-11-12)

Surveyed 28 top job-automation repos (largest: career-ops 81.9k★, ai-job-search 71.2k★, awesome-remote-job 50.5k★, AIHawk 32.5k★, SimplifyJobs New-Grad 18.4k★, JobSpy 4.7k★; also ever-jobs, jobseek, kalil0321/ats-scrapers, openings-mcp, speedyapply, Hasjob).
Many of them scrape LinkedIn / Indeed / Naukri / Glassdoor — those stay link-out only here.

### Verdicts for new candidates

| Source | Verdict | Endpoint | Notes / duties | Relevance |
|---|---|---|---|---|
| HN "Who is hiring" | OK-API | Firebase `v0/user/whoishiring.json` → Algolia `hn.algolia.com/api/v1/items/{id}` (whole thread, 1 call) | free-text posts → needs a parser; ~225 posts/month, ~30 junior, ~6 India | High |
| speedyapply `NEW_GRAD_INTL.md` | OK (raw GitHub) | raw markdown table | no licence → credit the repo; ~116 India new-grad rows | High |
| SimplifyJobs `listings.json` | OK (raw GitHub) | `.github/scripts/listings.json` (~13 MB) | no licence → credit; US-centric; best used to harvest employer ATS slugs | Medium |
| Jobicy | Attribution | `/api/v2/remote-jobs?geo=apac` | credit with a link; Apply → original URL | Medium |
| realworkfromanywhere | OK-RSS | `/rss.xml` | worldwide-only roles (fits the India-eligibility rule); `/go/` disallowed | Medium-High |
| 4dayweek | Attribution | `/api/v2` (robots allows v1/v2, disallows `/api/`) | link back to 4dayweek.io | Medium |
| CryptoJobsList | OK-RSS | `api.cryptojobslist.com/jobs.rss` | terms page unverified | Medium |
| Hasjob | OK-Atom | `hasjob.co/feed` | India-only, low volume | Medium |
| Remote.com | OK-JSON | `apply.remote.com/api/public/jobs` | has pay + region | Low-Medium |
| Arbeitnow | Attribution | `/api/job-board-api` | Germany-heavy | Low |
| NoDesk | OK-RSS | `/remote-jobs/index.xml` | 10 items | Low |
| Remotive | **robots disallow `/api/*`** | — | not exempted (only ~13 dev jobs) | Low |
| remotefirstjobs | robots unreadable (403) | `/remote-jobs.rss` | needs an exemption decision | Medium |
| Findwork, Web3.career, Jooble, Careerjet, Reed | Key required | — | free keys need the user to sign up | Low-Medium |
| The Muse, startup.jobs, echojobs | Dead for us (Cloudflare / bot wall) | — | never bypass | — |
| USAJobs, Landing.jobs, Breezy | DO-NOT-AUTOMATE (robots) | — | link-out | — |
| Internshala, Cutshort, Instahyre, Hirist/iimjobs, Foundit, freehire.me | DO-NOT-AUTOMATE (terms / no API / scraped data) | — | link-out | — |

### More ATS types with public board endpoints

| ATS | Endpoint | robots |
|---|---|---|
| Recruitee | `{co}.recruitee.com/api/offers/` | allows (only `/v/` disallowed) |
| Workable | `apply.workable.com/api/v1/widget/accounts/{co}` | allows |
| Rippling | `ats.rippling.com/api/v2/board/{slug}/jobs` | allows (only `/internal/`) |
| Teamtailor | `{co}.teamtailor.com/jobs.rss` | allows |
| Pinpoint | `{co}.pinpointhq.com/postings.json` | allows |
| Personio | `{co}.jobs.personio.(de\|com)/xml` | allows |
| BambooHR | `{co}.bamboohr.com/careers/list` | allows |
| Keka (India) | `{t}.keka.com/careers/...` | `Allow: /careers` — verify endpoint |
| Darwinbox (India) | — | robots unreadable (302) → needs an exemption decision |

### Employer job-board datasets (feed the existing ATS connectors)

| Dataset | Licence | Size |
|---|---|---|
| kalil0321/ats-scrapers `ats-companies/*.csv` | MIT | Greenhouse 6,031 · Workday 3,530 · Ashby 3,448 · Lever 2,402 · SmartRecruiters 2,747 · Workable 4,752 · Recruitee 1,164 · Rippling 1,923 · Teamtailor 1,464 · Pinpoint 406 · Keka 185 · … |
| colophon-group/jobseek `boards.csv` | MIT (code) | 8,307 boards |
| zshah101 `data/companies.json` | MIT | 5,355 |
| amikai/openings-mcp `companies.yaml` per ATS | MIT | ~1–4k per ATS |
| slugs from SimplifyJobs listing URLs | none (facts only) | ~2,400 new-grad-hiring employers |

None carry country tags, and polling ~30k boards daily would break the low-volume rule, so the plan is a **rotating India probe**: up to **4000 boards per run**, each board's list endpoint called once, re-probed after 7 days (so the seed set cycles in about a week), keeping boards that show at least one relevant India / APAC / worldwide-remote role, prioritised by overlap with new-grad lists, yc-oss and Bangalore Startup Map, and queued only if the board's company tier is one live ingest accepts (`co.ingest_tiers()`: `companies.require_tier` plus the extended `watchlist` lane). The 4000 total is not the politeness limit; per-system caps are: **1 req/s per platform** (every `<tenant>.recruitee.com`, `*.myworkdayjobs.com`, teamtailor, pinpoint, personio, bamboohr, keka tenant shares one token bucket in `PoliteClient`, robots.txt fetches included; shared-host ATSs such as Greenhouse, Lever and Ashby are 1 req/s per host), at most **300 boards per run per subdomain platform** (`probe.max_per_platform_per_run`) and 3000 per other host. A host or platform that answers 429 or asks for a long wait is dropped for the run. A transient failure (5xx, 429, 401/403, timeout) never demotes a board: it is retried after ~1 day and demoted only after 3 failures in a row, or at once on 404 / 410.
