# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ROLYMOLY_DATABASE_TARGET=supabase://rolymoly \
    ROLYMOLY_DATA_DIR=/var/lib/rolymoly

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 rolymoly \
    && useradd --uid 10001 --gid 10001 --create-home rolymoly \
    && mkdir -p /app/.streamlit /var/lib/rolymoly /home/rolymoly/.streamlit \
    && chown -R 10001:10001 /var/lib/rolymoly /home/rolymoly

# Explicit source paths plus .dockerignore keep private files out of layers.
COPY app.py streamlit_app.py ./
COPY roly/ ./roly/
COPY app_pages/ ./app_pages/
COPY scripts/ ./scripts/
COPY static/ ./static/
COPY .streamlit/config.toml ./.streamlit/config.toml

USER 10001:10001
EXPOSE 8501
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"]

# One launcher starts the existing persistent settlement worker and one app.
ENTRYPOINT ["python", "-m", "roly.server"]
CMD ["--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--server.fileWatcherType=none"]
