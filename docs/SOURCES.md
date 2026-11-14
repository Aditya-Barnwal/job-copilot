# Job sources: access rules

Before any source is used, it goes through the same gate: is there an official
API, a feed or a public JSON endpoint; what does robots.txt say; what do the
terms say; and what attribution or storage duties come with it. This page
records the verdict and the reasoning for each source. The settings that
implement these verdicts live in [`config/sources.yaml`](../config/sources.yaml)
(boards, HTTP politeness, robots exemptions, denylist) and
[`config/seeds.yaml`](../config/seeds.yaml) (seed datasets and the probe). The
client that enforces them is [`jobcopilot/http.py`](../jobcopilot/http.py); see
[`DESIGN.md`](DESIGN.md#3-the-honest-http-client).

Sources were first checked in November 2026 with an honest user agent and a
handful of requests per host. Terms change, so a source is re-checked when it
starts failing, and periodically anyway. Anything marked UNVERIFIED was not
confirmed first-hand.

## Verdict key

| Verdict | Meaning |
|---|---|
| OK-API / OK-RSS / OK-JSON | an official or documented public endpoint, allowed by robots.txt |
| Attribution | allowed, with a duty to credit and link back (shown on every card) |
| Key required | needs a free API key the user signs up for; skipped when absent |
| robots disallow | robots.txt forbids the endpoint we'd need, so it isn't used |
| DO-NOT-AUTOMATE | the terms forbid automated access: link out only, a human applies |
| Dead for us | behind a bot wall; never bypassed |

## Core sources

| Source | Verdict | How we ingest | Must-dos |
|---|---|---|---|
| Greenhouse / Lever / Ashby / Workday CXS | OK-API (public board APIs) | ATS connectors | rate-limited per host, plus a shared per-platform bucket |
| SmartRecruiters | **robots disallow** (`User-agent: * Disallow: /`; only one named crawler is allowed) | connector built, not used | employers on it stay manual; not exempted |
| Gem | OK-API; robots.txt unreadable (403) | `api.gem.com/job_board/v0/{slug}/job_posts/` | exempted as a documented public job-board API, with the reason in `http.robots_exempt_hosts`; still rate-limited |
| **Himalayas** | OK-API + **attribution** | `/jobs/api/search` (filtered queries, first page only) | visible "via Himalayas" link; **Apply must use `applicationLink`**; drop jobs after `expiryDate`; no bulk collection. An optional free key raises the source's own limits. |
| **Working Nomads** | OK-API | `/api/exposed_jobs/` (recent jobs, no paging) | no resale or competing product |
| **Jobspresso** | OK-RSS, personal use only | `/feed/job_feed/` (the path form; the `?feed=` form is robots-disallowed) | the feed holds only the newest few posts, so it's polled on a short interval (`rss_poll_hours`, `config/schedule.yaml`); minimal fields + link; no HTML scraping |
| **RemoteOK** | OK-API + **attribution** | `/api` (element 0 is a legal notice) | followable link back and "Remote OK" named as the source on the screen showing the data; Crawl-delay honoured; no logo |
| **We Work Remotely** | OK-RSS + **attribution** | category RSS feeds | attribute links; RSS only, since the HTML is Cloudflare-challenged and the API terms forbid storing (UNVERIFIED exact wording), so minimal fields + link |
| **YC Work at a Startup / ycombinator.com** | **DO-NOT-AUTOMATE** | none | ycombinator.com/legal applies "including all subdomains": *"you will not engage in or use any data mining, robots, scraping or similar data gathering or extraction methods"*. Link out only; a human applies. |
| yc-oss API (`yc-oss.github.io/api`) | OK-API, unofficial | `companies/hiring.json` → company websites → ATS detector | built from YC's public index by a GitHub Action; **no licence**, so its standing is unclear; used only to discover company names, websites and batch, never jobs |
| **Bangalore Startup Map** | Attribution; robots.txt allows the homepage and company pages; no terms page found | refreshed in the nightly run: one conditional GET of the public homepage payload (companies with stage, sector, website and a hiring map), then company pages only for hiring companies whose roles changed, slowly and capped per run (`discovery.bangalore_startup_map`, `http.host_rates`) | **never call `/api/`** (robots-disallowed); don't mirror their role feed: company pages only reveal which ATS a company uses, and jobs come from the ATS APIs |
| LinkedIn, Naukri, Indeed, Glassdoor, Wellfound (and any site whose terms prohibit automation) | DO-NOT-AUTOMATE | none | link out only; enforced by `http.denied_hosts` and a built-in list |

## robots.txt on the API hosts we call

| Host | robots.txt | Effect |
|---|---|---|
| boards-api.greenhouse.io | 200 · `Disallow: /embed/` | `/v1/boards/…` allowed |
| api.lever.co | 200 · `Allow: /`, with a `Crawl-delay` | allowed; the delay is honoured automatically |
| *.myworkdayjobs.com | 200 · `Allow: /jobs/`, `Disallow: /refreshFacet/` | CXS `/wday/cxs/…` allowed |
| api.ashbyhq.com | **401** (unreadable) | exempted in `config/sources.yaml`: a documented public Job Postings API built for job boards |
| api.adzuna.com | 200 · `Disallow: /` | **not** exempted (key-gated, unused); decide explicitly before adding keys |
| jsearch.p.rapidapi.com | 200 · `Disallow: /` | **not** exempted (key-gated, unused) |

`jobcopilot/http.py` enforces robots.txt for every other host (RFC 9309
longest match). An unreadable robots.txt (401, 403, 5xx, network error) counts
as "disallow" unless the host is exempted with a written reason, because an auth
wall is not consent. `Crawl-delay` is honoured.

## Field coverage

What each source gives us matters as much as whether we may use it: the
geography rule needs region data, and the salary rule needs pay.

| Source | Salary | Time zone / region | Posted date | Description |
|---|---|---|---|---|
| Himalayas | `minSalary/maxSalary/currency/salaryPeriod` (often null) | `locationRestrictions[]`, `timezoneRestrictions[]` (UTC offsets) | `pubDate` epoch | HTML |
| Working Nomads | none | free-text `location` ("Europe, LATAM, APAC…") | ISO | HTML |
| Jobspresso | text (`job_listing:salary`) | free-text location | `pubDate` | `content:encoded` |
| RemoteOK | `salary_min/max` (zero means none) | free-text `location` (often empty) | epoch | HTML |
| We Work Remotely | free text in the body | `region` ("USA Only", "Anywhere in the World") | `pubDate`, `expires_at` | HTML |
| Ashby | `compensationTierSummary` when the employer displays it | `isRemote`, location | `publishedAt` | HTML |
| Greenhouse | list endpoint: none (detail UNVERIFIED) | location text | `updated_at` | `content=true` |

## ATS detection

Hand-written ATS guesses go stale quickly. Of the companies the first version
was configured with by hand, many failed for one of a few reasons:

- the company moved to a different ATS (Greenhouse to Ashby, Lever to Greenhouse);
- the board slug isn't the company name (a legal-entity name or a numeric suffix);
- the company rebranded and its board moved with it;
- the careers page wraps the real ATS in a custom front end or an iframe;
- the company uses a regional API host, or an ATS with no public board API;
- the company moved to a proprietary portal, which stays manual.

So `jobcopilot/ats_detect.py` reads the careers page and looks for
fingerprints, in this order:

1. Final URL after redirects (`jobs.ashbyhq.com/<slug>`, `jobs.lever.co/<slug>`, `*.myworkdayjobs.com`).
2. Regexes over href, src and raw HTML:
   - Ashby `jobs\.ashbyhq\.com/([\w.-]+)`
   - Greenhouse `(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_(?:board/js|app)\?for=)?([\w-]+)`; `[?&]gh_jid=\d+` ⇒ Greenhouse, slug from one detail page's `for=`
   - Workday `([\w-]+)\.wd(\d+)\.myworkdayjobs\.com/([\w-]+)` (also inside JSON `"applyUrl"`)
   - Lever `jobs\.lever\.co/([\w-]+)`, SmartRecruiters `careers\.smartrecruiters\.com/(\w+)`, Gem `jobs\.gem\.com/([\w-]+)`, TurboHire `([\w-]+)\.turbohire\.co`
   - Phenom (`cdn.phenompeople.com`, `"refNum"`) is a front end, so read `applyUrl` on its search page to find the real ATS.
3. Follow `<iframe src>` and grep its JS for `boards-api.greenhouse.io` / `BOARD_TOKEN` (a custom single-page careers site often embeds the board token in its bundle).
4. Still nothing: follow one job-detail link; otherwise mark the company `manual`.
5. Always confirm with one API call to the endpoint the connector will use; on 429 back off, never loop.

## Wider survey of job sources

To find sources beyond the obvious ones, the top-starred open-source
job-automation and job-aggregation repositories on GitHub were surveyed for
every job source they use (among them career-ops, ai-job-search,
awesome-remote-job, AIHawk, SimplifyJobs New-Grad-Positions, JobSpy,
ever-jobs, jobseek, kalil0321/ats-scrapers, openings-mcp, speedyapply and
Hasjob). Many of them scrape LinkedIn, Indeed, Naukri or Glassdoor; those
stay link-out only here, however popular the scraper.

### Verdicts for new candidates

| Source | Verdict | Endpoint | Notes and duties |
|---|---|---|---|
| HN "Who is hiring" | OK-API | Firebase `v0/user/whoishiring.json` → Algolia `hn.algolia.com/api/v1/items/{id}` (the whole thread in one call) | free-text posts, so it needs a parser (`jobcopilot/hn_parse.py`); bounded per run in `boards.hn_hiring` |
| speedyapply `NEW_GRAD_INTL.md` | OK (raw GitHub) | raw markdown table | no licence, so facts only, credited; strong for new-grad roles |
| SimplifyJobs `listings.json` | OK (raw GitHub) | `.github/scripts/listings.json` (large) | no licence, so facts only, credited; US-centric; most useful for harvesting employer ATS slugs |
| Jobicy | Attribution | `/api/v2/remote-jobs` (region filter) | credit with a link; Apply goes to the original URL |
| realworkfromanywhere | OK-RSS | `/rss.xml` | worldwide-only roles, which suit the remote geography rule; `/go/` is robots-disallowed and never followed |
| 4dayweek | Attribution | `/api/v2` (robots allows v1 and v2, disallows `/api/`) | link back to 4dayweek.io |
| CryptoJobsList | OK-RSS | `api.cryptojobslist.com/jobs.rss` | terms page unverified |
| Hasjob | OK-Atom | `hasjob.co/feed` | India-only, low volume |
| Remote.com | OK-JSON | `apply.remote.com/api/public/jobs` | has pay and region |
| Arbeitnow | Attribution | `/api/job-board-api` | Germany-heavy; remote roles only |
| NoDesk | OK-RSS | `/remote-jobs/index.xml` | small feed; its robots content signal allows reference use, not AI training |
| Remotive | **robots disallow `/api/*`** | none | not exempted |
| remotefirstjobs | robots unreadable (403) | `/remote-jobs.rss` | would need an exemption decision; not used |
| Findwork, Web3.career, Jooble, Careerjet, Reed | Key required | none | free keys need the user to sign up |
| The Muse, startup.jobs, echojobs | Dead for us (Cloudflare or bot wall) | none | never bypassed |
| USAJobs, Landing.jobs, Breezy | DO-NOT-AUTOMATE (robots) | none | link out |
| Internshala, Cutshort, Instahyre, Hirist/iimjobs, Foundit, freehire.me | DO-NOT-AUTOMATE (terms, no API, or scraped data) | none | link out |

### More ATS types with public board endpoints

| ATS | Endpoint | robots.txt |
|---|---|---|
| Recruitee | `{co}.recruitee.com/api/offers/` | allows (only `/v/` disallowed) |
| Workable | `apply.workable.com/api/v1/widget/accounts/{co}` | allows |
| Rippling | `ats.rippling.com/api/v2/board/{slug}/jobs` | allows (only `/internal/`) |
| Teamtailor | `{co}.teamtailor.com/jobs.rss` | allows |
| Pinpoint | `{co}.pinpointhq.com/postings.json` | allows |
| Personio | `{co}.jobs.personio.(de\|com)/xml` | allows |
| BambooHR | `{co}.bamboohr.com/careers/list` | allows |
| Keka | `{t}.keka.com/careers/...` | `Allow: /careers`; endpoint to be verified |
| Darwinbox | none found | robots unreadable (302), so it would need an exemption decision |

### Employer job-board datasets

These feed the existing ATS connectors with "company X uses ATS Y with board
Z" rows. They are imported as *unverified* until the probe confirms them
(see below and [`DESIGN.md`](DESIGN.md#4-discovery-finding-employers-and-their-boards)).

| Dataset | Licence | Used | Contents |
|---|---|---|---|
| [kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers) `ats-companies/*.csv` | MIT | yes | companies per ATS, across many ATS types |
| [colophon-group/jobseek](https://github.com/colophon-group/jobseek) `boards.csv` | MIT (code) | yes | employer boards with their monitor type |
| [zshah101 internships list](https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships) `data/companies.json` | MIT | yes | companies with ATS and board |
| [SimplifyJobs New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) listing URLs | none (facts only) | yes | employers hiring new grads, with their apply URLs |
| [amikai/openings-mcp](https://github.com/amikai/openings-mcp) `companies.yaml` per ATS | MIT | no (overlaps the above) | companies per ATS |

Every dataset is a static file on raw.githubusercontent.com (robots.txt there
answers 404, which means allowed), fetched with a conditional GET, so an
unchanged file costs one cheap request. Datasets without a licence give facts
only (name and board slug) and are credited by name.

### Why a rotating probe

Together the datasets name tens of thousands of boards, none with location
tags, so polling them all every day would break the low-volume rule. Instead
each run verifies a bounded slice (`probe.limit` in `config/seeds.yaml`, and
`daily_probe_limit` in `config/schedule.yaml` for the nightly run):

- each due board's list endpoint is called **once**, through its own connector;
- a board is kept only if it shows at least one relevant role that the
  eligibility rules could accept (`probe.require_role_match`, `probe.skip_senior`);
- a probed board is re-probed after `probe.stale_days`, so the whole seed set
  cycles over time;
- boards that overlap with lists already trusted (`probe.priority_signals`:
  new-grad lists, YC, the startup map) go first, then brand and funded tiers
  (`probe.priority_tiers`); only boards whose company tier live ingest accepts
  are queued, since a board that can never be ingested isn't worth a request.

The slice size is not the politeness limit. The real bounds are per system:

- `PoliteClient` gives every host its own token bucket, and every platform
  family (all `<tenant>.recruitee.com`, `*.myworkdayjobs.com`, Teamtailor,
  Pinpoint, Personio, BambooHR and Keka tenants, plus shared-host ATSs such as
  Greenhouse, Lever and Ashby) also shares one bucket, robots.txt fetches
  included (`http.platform_rates`, `http.default_rate_per_sec`);
- a run sends at most `probe.max_per_platform_per_run` requests to one
  subdomain-per-company platform and `probe.max_per_host_per_run` to any other
  single host;
- a host or platform that answers 429 or asks for a long wait is dropped for
  the rest of the run.

A transient failure (5xx, 429, 401/403, timeout) never demotes a board: it is
retried after `probe.retry_after_days` and demoted only after
`probe.max_consecutive_failures` failures in a row, or at once on 404 / 410.
