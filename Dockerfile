# job-copilot API + pipeline image.
# Personal data never goes in the image: profile/, data/ and .env are excluded
# (.dockerignore) and mounted at runtime by compose.yaml when running locally.
# Browser autofill is local-only and deliberately not installed here.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JOBCOPILOT_DB=/app/data/jobcopilot.db

WORKDIR /app

# CPU-only torch keeps the image ~1.5 GB smaller than the default CUDA wheel.
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

COPY jobcopilot/ jobcopilot/
COPY server/ server/
COPY config/ config/
COPY run.py .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/meta', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
