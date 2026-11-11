# Job sources — access rules

Checked **2026-11-11** with an honest UA (`job-copilot-research/0.1`), ≤ ~6 requests per host.
Re-check terms when a source starts failing or every ~6 months. Anything marked UNVERIFIED was not confirmed first-hand.

## Verdicts

| Source | Verdict | How we ingest | Must-dos |
|---|---|---|---|
| Greenhouse / Lever / Ashby / Workday CXS | OK-API (public board APIs) | existing connectors | per-host rate limit |
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
