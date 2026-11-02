# Pinned by digest so every build starts from the same image; Dependabot proposes updates.
FROM python:3.15.2-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv \
    PYTHONNOUSERSITE=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7

WORKDIR /srv

# Exact versions with hashes (requirements.txt is generated from pyproject.toml, see its header).
# Every package has a binary wheel for this Python, so nothing is compiled or fetched unhashed.
COPY requirements.txt ./
RUN pip install --require-hashes --only-binary :all: -r requirements.txt

# The app runs from /srv/app (PYTHONPATH), so it is not pip-installed: that would fetch an
# unpinned build backend. The code stays owned by root and is read-only for the runtime user;
# only /srv/data (SQLite, if used) is writable.
COPY app ./app
# No home directory and no user site-packages: nothing the runtime user can write is ever
# imported, also when the container runs without compose's read_only.
RUN useradd --no-create-home --home-dir /nonexistent --uid 1000 crypie \
    && mkdir -p /srv/data && chown crypie:crypie /srv/data && chmod 700 /srv/data
USER crypie

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# --proxy-headers: behind Coolify's proxy the app must know the request was https. Only the
# proxies in FORWARDED_ALLOW_IPS (env, see .env.example) may set X-Forwarded-For/-Proto; the
# client IP is the right-most address in X-Forwarded-For that is not one of them.
# --ws none: the app has no websockets. --limit-concurrency answers 503 beyond 64 open
# connections/requests instead of piling them up. Uvicorn has no read timeout for request
# bodies, so 64 slow uploads can hold every slot (also /health) until the proxy cuts them:
# keep Traefik's entrypoint readTimeout short (see README); the trading loop is unaffected.
# --workers 1: the trading engine, its locks and the schedulers live in one process (a
# WEB_CONCURRENCY env var must not change that).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--ws", "none", "--limit-concurrency", "64", "--timeout-keep-alive", "5", "--workers", "1"]
