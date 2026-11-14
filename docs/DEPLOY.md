# Deploying the public demo (Google Cloud Run)

One **demo** instance can run on Google Cloud Run. It holds fixture data and a
fake person only. The real Job-Copilot keeps running on the owner's own
machine: the profile, database and signed-in browser never leave it, and
autofill needs that local browser anyway. The reasoning is in
[`DESIGN.md`](DESIGN.md#15-privacy-the-llm-and-the-public-demo).

## What is deployed

- The FastAPI app with the normal UI, started with `JOBCOPILOT_DEMO=1`
  ([`jobcopilot/demo.py`](../jobcopilot/demo.py)). On every start it builds a
  throwaway SQLite DB in `/tmp` from the committed fixtures (`tests/fixtures`),
  using the same offline pipeline as `make daily-fixtures`.
- The profile is `profile.example/`, the made-up "Alex Example".
- Every page has a banner: **"Demo — sample data, not a real job seeker"**.
- Visitors can browse jobs, the queue, fit breakdowns and the tracker, and can
  change tracker statuses. Those changes go to the throwaway DB and are lost
  whenever the instance restarts, which happens after a short idle period
  because the service scales to zero.
- Switched off with `403 "demo: disabled …"`:
  - tailoring, cover letters, emails and answer previews (they need an LLM and a
    real résumé)
  - uploads, profile edits, ingest, discovery, "run now", funding and autofill
- At runtime the demo has no network and no LLM:
  - `socket.getaddrinfo` / `connect` refuse every non-loopback host
  - LLM calls raise "demo: disabled"
  - the embedding model is never loaded, so torch is never imported
- The image is slim (`requirements-demo.txt`): no torch,
  sentence-transformers, playwright or openai.

## What never leaves the local machine

`profile/` (résumé facts, answers, base résumés), `.env` (API keys), `data/`
(the real DB, PDFs, logs), `jobs.jsonl`, `discovered_companies.yaml` and browser
sessions.

Three layers enforce this:

1. **Upload staging.** [`scripts/deploy_cloudrun.sh`](../scripts/deploy_cloudrun.sh)
   does not upload the repo. It copies a fixed allow-list into a temp dir,
   filtered through `.gitignore`:
   - `jobcopilot/`, `server/`, `config/`
   - `tests/fixtures/`, `profile.example/`
   - `Dockerfile`, `.dockerignore`, `requirements*.txt`, `run.py`

   It then refuses to continue if any `profile/`, `data/`, `.env*`, `*.db` or
   `*.pdf` file got in. To see exactly what would be uploaded, run
   `scripts/deploy_cloudrun.sh stage /tmp/jc-stage`.
2. **`.dockerignore`.** It excludes the same paths and `**/*.db` / `*.pdf`.
3. **Demo mode itself.** It never reads `.env` and points the profile at a temp
   copy of `profile.example/`.

[`tests/test_demo.py`](../tests/test_demo.py) checks all three. One of its
checks runs a clean interpreter with audit hooks and fails if the demo opens
`profile/` or `.env`, or resolves or connects to a non-local host.

## Cost model

The demo is designed to fit in the free allowances of a low-traffic project:
request-based billing, scale to zero, at most one instance, a small image, and
old images pruned. Free tiers change and apply per billing account, so **check
the pricing pages** before deploying:

| Service | What the demo uses | Pricing |
|---|---|---|
| Cloud Run | a small CPU / memory size, request-based billing, zero to one instance | https://cloud.google.com/run/pricing |
| Cloud Build | one build per deploy | https://cloud.google.com/build/pricing |
| Artifact Registry | the image; the script keeps only the newest few (`KEEP_IMAGES`) | https://cloud.google.com/artifact-registry/pricing |
| Cloud Storage | `--source` uploads a small tarball to `gs://<project>_cloudbuild` per deploy; delete old tarballs if you deploy often | https://cloud.google.com/storage/pricing |

- **Region.** The script defaults to `us-central1` (`REGION`). Always-free tiers
  are tied to some US regions; check each pricing page.
- **Billing account.** Cloud Run needs one linked even when usage stays free.
  Set the budget alert below so an email arrives before anything costs money.

## Step by step

1. **Install the Google Cloud SDK.**
   - Download the macOS archive from https://cloud.google.com/sdk/docs/install,
     unpack it to `~/google-cloud-sdk`, and run `./google-cloud-sdk/install.sh`;
     or use Homebrew (`brew install --cask google-cloud-sdk`) once
     `sudo xcodebuild -license` is accepted.
   - The script looks for gcloud in this order: `$GCLOUD`, then `gcloud` on
     PATH, then `~/google-cloud-sdk/bin/gcloud`, so it doesn't need to be on
     PATH.
2. **Log in** (opens the browser):

   ```sh
   gcloud auth login
   ```
3. **Create or choose a project.** Project IDs are global and lowercase.

   ```sh
   gcloud projects create <project-id>
   gcloud config set project <project-id>
   ```
4. **Link billing**, in the console at
   https://console.cloud.google.com/billing/linkedaccount or with:

   ```sh
   gcloud billing accounts list
   gcloud billing projects link <project-id> --billing-account=<billing-account-id>
   ```
5. **Set the budget alert** (next section) before the first deploy.
6. **Deploy.** The script asks before doing anything.

   ```sh
   make deploy-demo PROJECT=<project-id>
   # or: GOOGLE_CLOUD_PROJECT=<project-id> scripts/deploy_cloudrun.sh deploy
   ```

   The script does the following, and running it again is safe:
   - runs the local demo check first (`make demo-check`: build the image, run it
     like Cloud Run, smoke-test it)
   - enables `run.googleapis.com`, `cloudbuild.googleapis.com` and
     `artifactregistry.googleapis.com`
   - stages the upload dir (above)
   - runs `gcloud run deploy <service> --source <stage> --allow-unauthenticated
     --set-env-vars JOBCOPILOT_DEMO=1` with demo-sized resources, scale to zero
     and a single maximum instance (the exact flags are in
     `scripts/deploy_cloudrun.sh`)
   - prunes old images and prints the URL

   Each run creates a new revision. The first run creates the Artifact Registry
   repo `cloud-run-source-deploy` in the region.

   Build args: `gcloud run deploy --source` takes no `--build-arg`; Cloud Build
   builds the `Dockerfile` it finds in the source dir. So the staged copy of the
   Dockerfile gets the demo values as its ARG defaults (`REQS=requirements-demo.txt`,
   `DEMO=1`, an empty `TORCH_INDEX`), while the repo's Dockerfile keeps the full
   build as its default for `docker compose`.
7. **Get the URL later:** `make demo-url PROJECT=<project-id>`
8. **Stop:** `make stop-demo PROJECT=<project-id>`. It asks before deleting the
   service, then again before deleting its images
   (`gcloud artifacts packages delete <service> --repository cloud-run-source-deploy --location <region>`).
   Cloud Run costs nothing when idle, so stopping matters mostly for the stored
   image.

Knobs, set as env vars or `make VAR=…`: `REGION`, `SERVICE`, `KEEP_IMAGES`,
`GCLOUD`, and `YES=1` to skip the prompts.

Try it locally first with `make demo-local`: the same demo mode on
http://127.0.0.1:8000, from the venv.

## Budget alert

A budget emails the billing admins as spending crosses thresholds of a monthly
amount you choose. Run it after billing is linked; it may ask to enable
`billingbudgets.googleapis.com`.

```sh
gcloud billing budgets create \
  --billing-account=<billing-account-id> \
  --display-name="job-copilot demo" \
  --budget-amount=<amount>USD \
  --filter-projects=projects/<project-id> \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0
```

A budget only alerts; it never stops spending. To cap the demo hard, run
`make stop-demo`. The single maximum instance already limits how far it can
scale.

## Troubleshooting

- **"gcloud not found":** set `GCLOUD=/path/to/gcloud`, or see step 1.
- **"not logged in":** run `gcloud auth login`.
- **`--allow-unauthenticated` fails** under an organization policy (domain
  restricted sharing): use a personal project outside the org.
- **Build logs:**

  ```sh
  gcloud builds list --region <region>
  gcloud builds log <build-id> --region <region>
  ```
- **Runtime logs:** `gcloud run services logs read <service> --region <region>`.
