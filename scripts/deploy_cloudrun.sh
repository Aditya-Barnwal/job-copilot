#!/usr/bin/env bash
# Public DEMO of job-copilot on Google Cloud Run (fixture data + the fake
# profile.example only). See docs/DEPLOY.md.
#
#   scripts/deploy_cloudrun.sh deploy [PROJECT_ID]   build + deploy (idempotent: re-run = new revision)
#   scripts/deploy_cloudrun.sh url    [PROJECT_ID]   print the service URL
#   scripts/deploy_cloudrun.sh stop   [PROJECT_ID]   delete the service (+ optionally its images)
#   scripts/deploy_cloudrun.sh stage  DIR            only write the upload directory (no gcloud; for review/tests)
#
# PROJECT_ID: the argument, else $GOOGLE_CLOUD_PROJECT.
# Env knobs: REGION (us-central1), SERVICE (job-copilot-demo), KEEP_IMAGES (2), YES=1 (no prompts),
#   GCLOUD (path to gcloud; default: `command -v gcloud`, else ~/google-cloud-sdk/bin/gcloud).
#
# What is uploaded: NOT the repo. A fresh temp directory holding only the
# allow-listed paths below, filtered through .gitignore (so profile/, .env,
# data/, *.db, *.pdf can never be in it), then checked again before upload.
# `gcloud run deploy --source` has no build-arg flag, so the staged Dockerfile
# gets the demo values as its ARG defaults (REQS=requirements-demo.txt, DEMO=1).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-job-copilot-demo}"
KEEP_IMAGES="${KEEP_IMAGES:-2}"
AR_REPO="cloud-run-source-deploy"     # the repository `gcloud run deploy --source` creates/uses

# Everything the demo image needs, and nothing else.
ALLOW=(Dockerfile .dockerignore requirements.txt requirements-demo.txt run.py
       jobcopilot server config tests/fixtures profile.example)

die()  { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*" >&2; }

confirm() {
  [ "${YES:-}" = "1" ] && return 0
  local reply
  read -r -p "$1 [y/N] " reply || true
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

resolve_gcloud() {
  # $GCLOUD, else gcloud on PATH, else the default SDK install (not on PATH by default)
  if [ -n "${GCLOUD:-}" ]; then
    [ -x "$GCLOUD" ] || command -v "$GCLOUD" >/dev/null 2>&1 || die "GCLOUD=$GCLOUD is not an executable"
  elif command -v gcloud >/dev/null 2>&1; then
    GCLOUD="$(command -v gcloud)"
  elif [ -x "$HOME/google-cloud-sdk/bin/gcloud" ]; then
    GCLOUD="$HOME/google-cloud-sdk/bin/gcloud"
  else
    die "gcloud not found: install the Google Cloud SDK (docs/DEPLOY.md, step 1) or set GCLOUD=/path/to/gcloud"
  fi
}

need_gcloud() {
  resolve_gcloud
  info "using $GCLOUD"
  local acct
  acct="$("$GCLOUD" auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n1)"
  [ -n "$acct" ] || die "gcloud is not logged in. Run: $GCLOUD auth login"
  info "gcloud account: $acct"
}

project_id() {
  local p="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
  [ -n "$p" ] || die "no project id: pass it (make deploy-demo PROJECT=my-project) or export GOOGLE_CLOUD_PROJECT"
  echo "$p"
}

# --- staging --------------------------------------------------------------------

stage() {
  local dest="$1"
  [ -n "$dest" ] || die "stage: missing directory"
  mkdir -p "$dest"
  [ -z "$(ls -A "$dest")" ] || die "stage: $dest is not empty"
  command -v git >/dev/null 2>&1 || die "git is needed to apply .gitignore while staging"

  # tracked + untracked-but-not-ignored files under the allow-list
  (cd "$ROOT" && git ls-files -z --cached --others --exclude-standard -- "${ALLOW[@]}") |
  while IFS= read -r -d '' f; do
    [ -f "$ROOT/$f" ] || continue                     # deleted in the working tree
    case "$f" in */__pycache__/*|*.pyc) continue ;; esac
    mkdir -p "$dest/$(dirname "$f")"
    cp -p "$ROOT/$f" "$dest/$f"
  done

  # demo build: rewrite the ARG defaults (gcloud --source cannot pass --build-arg)
  local df="$dest/Dockerfile" tmp
  [ -f "$df" ] || die "stage: Dockerfile missing"
  tmp="$df.tmp"
  sed -e 's|^ARG REQS=.*|ARG REQS=requirements-demo.txt|' \
      -e 's|^ARG DEMO=.*|ARG DEMO=1|' \
      -e 's|^ARG TORCH_INDEX=.*|ARG TORCH_INDEX=|' "$df" > "$tmp" && mv "$tmp" "$df"
  grep -q '^ARG REQS=requirements-demo.txt$' "$df" || die "stage: could not set ARG REQS in Dockerfile"
  grep -q '^ARG DEMO=1$' "$df" || die "stage: could not set ARG DEMO in Dockerfile"
  grep -q '^ARG TORCH_INDEX=$' "$df" || die "stage: could not set ARG TORCH_INDEX in Dockerfile"

  # gcloud uploads everything not in .gcloudignore; the directory is already minimal
  printf '%s\n' '.gcloudignore' '**/__pycache__/' '**/*.pyc' > "$dest/.gcloudignore"

  # belt and braces: refuse if anything personal slipped in
  local bad
  bad="$(cd "$dest" && find . \( -path ./profile -o -path ./data -o -name '.env' -o -name '.env.*' \
           -o -name '*.db' -o -name '*.sqlite*' -o -name '*.pdf' -o -name '*.doc' -o -name '*.docx' \
           -o -name 'jobs.jsonl' -o -name 'discovered_companies.yaml' -o -name '.browser-profile' \) -print)"
  [ -z "$bad" ] || die "stage: refusing to upload personal files:
$bad"
  info "staged $(cd "$dest" && find . -type f | wc -l | tr -d ' ') files in $dest"
}

# --- commands ---------------------------------------------------------------------

service_url() {
  "$GCLOUD" run services describe "$SERVICE" --project "$1" --region "$REGION" \
    --format='value(status.url)' 2>/dev/null || true
}

prune_images() {
  # Keep the newest $KEEP_IMAGES images so Artifact Registry stays in the free 0.5 GB.
  local project="$1" img digests n=0
  img="${REGION}-docker.pkg.dev/${project}/${AR_REPO}/${SERVICE}"
  digests="$("$GCLOUD" artifacts docker images list "$img" --project "$project" \
               --sort-by='~updateTime' --format='value(version)' 2>/dev/null || true)"
  for d in $digests; do
    n=$((n + 1))
    [ "$n" -le "$KEEP_IMAGES" ] && continue
    info "pruning old image $img@$d"
    "$GCLOUD" artifacts docker images delete "$img@$d" --project "$project" --delete-tags --quiet \
      >/dev/null 2>&1 || echo "  (could not delete $d; delete it in the console if storage grows)" >&2
  done
}

cmd_deploy() {
  need_gcloud
  local project; project="$(project_id "${1:-}")"
  echo "About to deploy the PUBLIC DEMO (fixture data + fake profile only):"
  echo "  project=$project  region=$REGION  service=$SERVICE  (public URL, max 1 instance)"
  confirm "Continue?" || die "cancelled"

  # Pre-flight: build + run + smoke-test the exact demo image locally first, so a
  # broken image never reaches Cloud Run. Needs podman or docker; SKIP_DEMO_CHECK=1
  # skips it (e.g. on a machine without a container engine).
  if [[ "${SKIP_DEMO_CHECK:-0}" != "1" ]]; then
    if command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then
      info "pre-flight: local demo image check (scripts/demo_check.sh)"
      "$(dirname "$0")/demo_check.sh" || die "local demo check failed - not deploying"
    else
      info "pre-flight skipped: no podman/docker on this machine"
    fi
  fi

  info "enabling APIs (no-op when already on)"
  "$GCLOUD" services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    --project "$project"

  STAGE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/jobcopilot-demo-src.XXXXXX")"
  trap 'rm -rf "${STAGE_TMP:-}"' EXIT
  stage "$STAGE_TMP/src"

  info "building with Cloud Build + deploying to Cloud Run (a few minutes)"
  "$GCLOUD" run deploy "$SERVICE" \
    --source "$STAGE_TMP/src" \
    --project "$project" --region "$REGION" \
    --allow-unauthenticated \
    --set-env-vars JOBCOPILOT_DEMO=1 \
    --cpu 1 --memory 512Mi \
    --min-instances 0 --max-instances 1 \
    --concurrency 40 --timeout 60 \
    --cpu-boost \
    --quiet

  prune_images "$project"
  local url; url="$(service_url "$project")"
  echo
  echo "Demo URL: ${url:-<unknown: gcloud run services describe $SERVICE --region $REGION>}"
}

cmd_url() {
  need_gcloud
  local project; project="$(project_id "${1:-}")"
  local url; url="$(service_url "$project")"
  [ -n "$url" ] || die "service $SERVICE not found in $project/$REGION (not deployed?)"
  echo "$url"
}

cmd_stop() {
  need_gcloud
  local project; project="$(project_id "${1:-}")"
  if [ -n "$(service_url "$project")" ]; then
    if confirm "Delete Cloud Run service $SERVICE in $project/$REGION?"; then
      "$GCLOUD" run services delete "$SERVICE" --project "$project" --region "$REGION" --quiet
      info "service deleted"
    else
      info "service kept"
    fi
  else
    info "service $SERVICE not found in $project/$REGION (already stopped)"
  fi
  if confirm "Also delete its container images (Artifact Registry $AR_REPO/$SERVICE)?"; then
    "$GCLOUD" artifacts packages delete "$SERVICE" --repository "$AR_REPO" --location "$REGION" \
      --project "$project" --quiet || info "no images to delete"
  fi
}

case "${1:-}" in
  deploy) shift; cmd_deploy "${1:-}" ;;
  url)    shift; cmd_url "${1:-}" ;;
  stop)   shift; cmd_stop "${1:-}" ;;
  stage)  shift; stage "${1:-}" ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
