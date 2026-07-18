FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno is the JavaScript runtime yt-dlp uses (with yt-dlp-ejs) to solve
# YouTube's n-signature challenge; without it YouTube returns no formats.
COPY --from=denoland/deno:bin-2.9.3 /deno /usr/local/bin/deno
# Writable Deno cache for the non-root user.
ENV DENO_DIR=/tmp/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ server/
COPY static/ static/

RUN useradd --create-home app \
    && mkdir -p /data \
    && chown -R app:app /data /app

ENV DOWNLOAD_DIR=/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
