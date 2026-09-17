# Build context is the repository root so the web service ships the same engine the cron runs.
FROM python:3.12-slim

RUN useradd --uid 10003 --create-home --shell /usr/sbin/nologin app
WORKDIR /app

COPY vps/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY featherflight_book.py daxko_reservations.py /app/
COPY vps/app.py vps/metrics.py /app/
COPY serverless/index.html serverless/dashboard.html /app/static/

# The engine writes cookies and caches under ./.runtime when run as the CLI; the
# web service disables that, but the directory must exist and be private anyway.
RUN mkdir -p /app/.runtime /data && chown -R app:app /app /data && chmod 700 /app/.runtime
USER app
ENV PYTHONUNBUFFERED=1 EVENTS_FILE=/data/events.jsonl

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
