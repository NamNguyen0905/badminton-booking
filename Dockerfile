FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY featherflight_web_requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY featherflight_mini.py ./featherflight_mini.py

RUN mkdir -p /app/.runtime

EXPOSE 8501

CMD ["sh", "-c", "streamlit run featherflight_mini.py --server.address 0.0.0.0 --server.port ${PORT:-8501}"]
