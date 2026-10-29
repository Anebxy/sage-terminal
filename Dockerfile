FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir aiohttp==3.10.10 websockets==13.1
COPY sage.py strat.py bt.py daily_summary.py config.json bt_result.json ./
COPY web ./web
ENV TZ=America/New_York PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=10000
EXPOSE 10000
CMD ["python3","sage.py"]
