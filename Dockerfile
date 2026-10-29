FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir requests==2.32.3
COPY engine.py daily_summary.py config.json ./
COPY web ./web
ENV TZ=America/New_York PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=10000
EXPOSE 10000
CMD ["python3","engine.py"]
