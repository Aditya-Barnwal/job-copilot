# job-copilot API + pipeline image.
# Personal data never goes in the image: profile/, data/ and .env are excluded
# (.dockerignore) and mounted at runtime by compose.yaml when running locally.
# Browser autofill is local-only and deliberately not installed here.
#
# Two builds from this one file:
#   full (default, compose.yaml):  docker build .
#   public demo (Cloud Run):       docker build --build-arg REQS=requirements-demo.txt \
#                                    --build-arg DEMO=1 --build-arg TORCH_INDEX= .
#   scripts/deploy_cloudrun.sh stages a copy of this file with the demo values as
#   the ARG defaults, because `gcloud run deploy --source` takes no build args.
FROM python:3.13-slim

# requirements file to install; requirements-demo.txt leaves out torch/sentence-transformers
ARG REQS=requirements.txt
# 1 = public demo: fixture data, fake profile, no network, no LLM (jobcopilot/demo.py)
ARG DEMO=0
# CPU-only torch keeps the full image ~1.5 GB smaller than the default CUDA wheel.
# Empty for the demo (nothing to fetch from it).
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JOBCOPILOT_DB=/app/data/jobcopilot.db \
    JOBCOPILOT_DEMO=${DEMO} \
    PORT=8000

WORKDIR /app

COPY requirements.txt requirements-demo.txt ./
RUN pip install ${TORCH_INDEX:+--extra-index-url "$TORCH_INDEX"} -r "$REQS"

COPY jobcopilot/ jobcopilot/
COPY server/ server/
COPY config/ config/
COPY run.py .
# Fake data for demo mode and `python -m jobcopilot.daily --no-network`:
# committed fixtures + the fake "Alex Example" profile (both public in the repo).
COPY tests/fixtures/ tests/fixtures/
COPY profile.example/ profile.example/

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000
# Used by `docker run` / compose; Cloud Run ignores HEALTHCHECK (it probes $PORT).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/meta' % os.environ.get('PORT','8000'), timeout=4).status == 200 else 1)"

# Cloud Run sets PORT (8080); locally it stays 8000. One worker: the demo DB and
# the daily-run thread are per process. `exec` so uvicorn gets SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]
