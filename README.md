# job-copilot — ingestion layer (v1)

The ban-safe foundation of your job copilot: pull roles straight from companies'
**official ATS feeds**, normalize them, and filter to your scope (India any
work-type / outside-India remote-only, tech-or-fintech, above your salary floor where pay is
disclosed). No scraping, no auto-submit — those are the parts that get accounts
banned, so they're deliberately not here.

This is **Phase 1, step 1** from our plan: the `ingest → filter` half of
`ingest → filter → score → tailor → confirm → track`.

## What's in the box

```
job-copilot/
├── companies.yaml            # your seed list (slugs are GUESSES — verify them)
├── run.py                    # entrypoint:  python run.py [...]
├── requirements.txt
├── jobcopilot/
│   ├── models.py             # Job + Compensation (the normalized shape)
│   ├── normalize.py          # html→text, workplace-type & India/non-India inference
│   ├── filters.py            # geo + sector + salary-floor rules
│   ├── ingest.py             # orchestrator + CLI + summary table
│   └── connectors/
│       ├── base.py           # fetch_raw() + parse() contract
│       ├── greenhouse.py     # GET boards-api.greenhouse.io/v1/boards/{slug}/jobs
│       ├── lever.py          # GET api.lever.co/v0/postings/{slug}?mode=json
│       ├── ashby.py          # GET api.ashbyhq.com/posting-api/job-board/{slug}  (+ pay!)
│       └── workday.py        # POST {tenant}.wd{N}.myworkdayjobs.com/wday/cxs/.../jobs
└── tests/
    ├── test_parsers.py       # 7 offline tests
    └── fixtures/             # sample payloads + demo_companies.yaml
```

## Quick start

```bash
pip install -r requirements.txt

# 1) Prove it works with zero network (bundled sample data):
python run.py --companies tests/fixtures/demo_companies.yaml --fixtures tests/fixtures

# 2) Run the tests:
python -m pytest tests/ -q

# 3) Go live against your real seed list (after verifying slugs — see below):
python run.py
python run.py --sectors tech,fintech --min-lpa <your floor>
```

Output is a `jobs.jsonl` (one normalized role per line) plus a summary table.

## The one manual step: verify each company

Slugs, Workday tenants, and ATS choices change, so **every entry in
`companies.yaml` is a best guess flagged `verify: true`.** Confirm each in ~30s:

1. Open the company careers page, click any job, look at the Apply URL:

   | Apply URL contains            | connector  | what to record                          |
   |-------------------------------|------------|-----------------------------------------|
   | `boards.greenhouse.io/<slug>` | greenhouse | `slug`                                  |
   | `jobs.lever.co/<slug>`        | lever      | `slug`                                  |
   | `jobs.ashbyhq.com/<slug>`     | ashby      | `slug`                                  |
   | `<tenant>.wd{N}.myworkdayjobs.com/en-US/<site>` | workday | `tenant`, `wd_server` (wd1/wd3/wd5…), `site` |
   | anything else (amazon.jobs, careers.google.com, higher.gs.com…) | manual | handle by hand for now |

2. Set `verify: false` once it returns rows.

The four giants on your list (Google, Microsoft, Amazon, Oracle) plus Goldman,
JP Morgan, Wells Fargo and D.E. Shaw run proprietary portals with no clean
public feed — they're marked `manual`. That's expected, and as we discussed,
discovery was never the bottleneck for those names; tailoring + tracking is.

## How the filters behave (by design)

- **Geo:** India keeps remote/onsite/hybrid; outside India keeps **remote only**.
  Roles whose country can't be determined are **kept and flagged**
  (`country_uncertain`), never silently dropped.
- **Salary floor** (set in `config/filters.yaml`): roles with disclosed pay must clear it. Roles with
  **no disclosed pay are kept and flagged** `package_estimated: true` — because
  most postings don't disclose, and dropping them would throw away most good
  roles. Your confirm step (and the funding/Adzuna signal coming next) resolves
  those. Only Ashby currently hands us real numbers.
- **Salary conversion is approximate:** USD→INR uses a flat rate (`--usd-inr`,
  default 83) and annualizes hourly/monthly intervals heuristically. Treat
  cross-currency figures as estimates.

## Adding a new ATS later

Write one class in `connectors/` with `fetch_raw()` + `parse()`, add one line to
`connectors/__init__.py:REGISTRY`. Every Workday company already shares the one
Workday connector — you add tenants in config, not code.

## What's next (not in this drop)

1. **Adzuna connector** — sanctioned salary bands + coverage for the big-tech
   names the ATS feeds miss.
2. **Funding watcher** — RSS→LLM over Inc42/Entrackr/YC to auto-add well-funded
   startups.
3. **Match scoring** — embed each role vs. your resume (the AI/ML core).
4. **Resume tailoring** — per-role rewrite with a visible diff.
5. **Confirm + tracker UI** — the React dashboard with the approve gate.
```
