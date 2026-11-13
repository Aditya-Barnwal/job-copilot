# Deploying the public demo (Google Cloud Run)

PLAN.md D13: one **demo** instance runs on Google Cloud Run. It holds fixture data
and a fake person only. Your real job-copilot keeps running on your Mac.

## What is deployed

- The FastAPI app with the normal UI, started with `JOBCOPILOT_DEMO=1`
  (`jobcopilot/demo.py`). On every start it builds a throwaway SQLite DB in `/tmp`
  from the committed fixtures (`tests/fixtures`). It uses the same offline pipeline
  as `make daily-fixtures`, which takes about a second.
- The profile is `profile.example/`, the made-up "Alex Example".
- Every page has a banner: **"Demo — sample data, not a real job seeker"**.
- Visitors can browse jobs, the queue, fit breakdowns and the tracker. They can
  change tracker statuses. Those changes go to the throwaway DB and are lost
  whenever the instance restarts. That happens after a few idle minutes, because
  min-instances is 0.
- Switched off with `403 "demo: disabled …"`:
  - tailoring, cover letters, emails and answer previews (they need an LLM and a
    real résumé)
  - uploads, profile edits, ingest, discovery, "run now" and funding
- At runtime the demo has no network and no LLM:
  - `socket.getaddrinfo`/`connect` refuse every non-loopback host
  - LLM calls raise "demo: disabled"
  - the embedding model is never loaded, so torch is never imported
- The image is slim (`requirements-demo.txt`, about 200 MB). It has no torch,
  sentence-transformers, playwright or openai.

## What never leaves your Mac

`profile/` (résumé facts, answers, base résumés), `.env` (API keys), `data/` (your
real DB, PDFs, logs), `jobs.jsonl`, `discovered_companies.yaml` and browser
sessions.

Three layers enforce this:

1. **Upload staging.** `scripts/deploy_cloudrun.sh` does not upload the repo. It
   copies a fixed allow-list into a temp dir, filtered through `.gitignore`:
   - `jobcopilot/`, `server/`, `config/`
   - `tests/fixtures/`, `profile.example/`
   - `Dockerfile`, `.dockerignore`, `requirements*.txt`, `run.py`

   It then refuses to continue if any `profile/`, `data/`, `.env*`, `*.db` or
   `*.pdf` file got in. To see exactly what would be uploaded, run
   `scripts/deploy_cloudrun.sh stage /tmp/jc-stage`.
2. **`.dockerignore`.** It excludes the same paths and `**/*.db`/`*.pdf`.
3. **Demo mode itself.** It never reads `.env` and points the profile at a temp
   copy of `profile.example/`.

`tests/test_demo.py` checks all three. One of its checks runs a clean interpreter
with audit hooks and fails if the demo opens `profile/`/`.env` or resolves or
connects to a non-local host.

## Cost

Expect $0 for a low-traffic demo. **Verify on the pricing pages.** Free tiers
change, and they apply per billing account.

| Service | What we use | Free allowance (verify on the page) |
|---|---|---|
| Cloud Run: https://cloud.google.com/run/pricing | 1 vCPU / 512 MiB, request-based billing, 0 to 1 instances | 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests per month |
| Cloud Build: https://cloud.google.com/build/pricing | one build per deploy, a few minutes | 2,500 build-minutes per month (e2-standard-2) |
| Artifact Registry: https://cloud.google.com/artifact-registry/pricing | the image, about 70 MB compressed | 0.5 GB storage per month. The script keeps only the 2 newest images (`KEEP_IMAGES`). |
| Cloud Storage: https://cloud.google.com/storage/pricing | `--source` uploads a ~0.5 MB tarball to `gs://<project>_cloudbuild` per deploy | 5 GB-month in US regions. Verify, and delete old tarballs if you deploy often. |

- **Region.** Use `us-central1`, the default. The always-free tiers are tied to
  some US regions; check each pricing page.
- **Billing account.** Cloud Run needs one linked even when usage stays free.
  Set the budget alert below so you get an email before anything costs money.
- **Old plan for comparison.** AWS ECS Fargate was about $9 per month while running.

## Step by step (you run these; nothing has been deployed)

1. **gcloud.** The Google Cloud SDK is already installed at
   `~/google-cloud-sdk/bin/gcloud`. It is not on your PATH, and you don't need
   Homebrew.
   - The script looks for gcloud in this order: `$GCLOUD`, then `gcloud` on PATH,
     then `~/google-cloud-sdk/bin/gcloud`.
   - Optional, for typing `gcloud` yourself: add
     `export PATH="$HOME/google-cloud-sdk/bin:$PATH"` to `~/.zshrc`.
   - Below, `gcloud` means `~/google-cloud-sdk/bin/gcloud` unless you did that.
   - To reinstall: download the macOS archive from
     https://cloud.google.com/sdk/docs/install, unpack it to `~/google-cloud-sdk`,
     and run `./google-cloud-sdk/install.sh`.
   - Homebrew (`brew install --cask google-cloud-sdk`) also works, but only after
     `sudo xcodebuild -license` is accepted.
2. **Log in** (opens the browser):

   ```sh
   ~/google-cloud-sdk/bin/gcloud auth login
   ```
3. **Create or choose a project.** Project IDs are global and lowercase.

   ```sh
   gcloud projects create job-copilot-demo-<suffix>
   gcloud config set project job-copilot-demo-<suffix>
   ```
4. **Link billing.** Do it in the console at
   https://console.cloud.google.com/billing/linkedaccount, or with these commands:

   ```sh
   gcloud billing accounts list
   gcloud billing projects link job-copilot-demo-<suffix> --billing-account=XXXXXX-XXXXXX-XXXXXX
   ```
5. **Set the budget alert** (next section) before the first deploy.
6. **Deploy.** The script asks before doing anything.

   ```sh
   make deploy-demo PROJECT=job-copilot-demo-<suffix>
   # or: GOOGLE_CLOUD_PROJECT=job-copilot-demo-<suffix> scripts/deploy_cloudrun.sh deploy
   ```

   The script does the following, and running it again is safe:
   - enables `run.googleapis.com`, `cloudbuild.googleapis.com` and
     `artifactregistry.googleapis.com`
   - stages the upload dir
   - runs `gcloud run deploy job-copilot-demo --source <stage> --region us-central1 --allow-unauthenticated --set-env-vars JOBCOPILOT_DEMO=1 --cpu 1 --memory 512Mi --min-instances 0 --max-instances 1 --concurrency 40 --timeout 60 --cpu-boost --quiet`
   - prunes old images and prints the URL

   Each run creates a new revision. The first run creates the Artifact Registry
   repo `cloud-run-source-deploy` in the region.

   Build args: `gcloud run deploy --source` takes no `--build-arg`. Cloud Build
   builds the `Dockerfile` it finds in the source dir. So the staged copy of the
   Dockerfile gets the demo values as its ARG defaults: `REQS=requirements-demo.txt`,
   `DEMO=1` and an empty `TORCH_INDEX`. The repo's Dockerfile keeps the full build
   as its default for `docker compose`.
7. **Get the URL later:** `make demo-url PROJECT=…`
8. **Stop:** `make stop-demo PROJECT=…`. It asks before deleting the service, then
   again before deleting its images (`gcloud artifacts packages delete job-copilot-demo --repository cloud-run-source-deploy --location us-central1`).
   Cloud Run costs nothing when idle, so stopping matters mostly for the stored image.

Knobs, set as env vars or `make VAR=…`: `REGION`, `SERVICE`, `KEEP_IMAGES`,
`GCLOUD`, and `YES=1` to skip the prompts.

Try it locally first with `make demo-local`. It runs the same demo mode on
http://127.0.0.1:8000 from your venv.

## Budget alert (document only; run it yourself)

This emails the billing admins at 50%, 90% and 100% of USD 1 per month. Run it
after billing is linked. It may ask to enable `billingbudgets.googleapis.com`.

```sh
gcloud billing budgets create \
  --billing-account=XXXXXX-XXXXXX-XXXXXX \
  --display-name="job-copilot demo (USD 1)" \
  --budget-amount=1USD \
  --filter-projects=projects/job-copilot-demo-<suffix> \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

A budget only alerts; it never stops spending. To cap the demo hard, run
`make stop-demo`. `--max-instances 1` already limits how much it can scale.

## Troubleshooting

- **"gcloud not found":** set `GCLOUD=/path/to/gcloud`, or see step 1.
- **"not logged in":** run `~/google-cloud-sdk/bin/gcloud auth login`.
- **`--allow-unauthenticated` fails** under an organization policy (domain
  restricted sharing): use a personal project outside the org.
- **Build logs:**

  ```sh
  gcloud builds list --region us-central1
  gcloud builds log <ID> --region us-central1
  ```
- **Runtime logs:** `gcloud run services logs read job-copilot-demo --region us-central1`.
