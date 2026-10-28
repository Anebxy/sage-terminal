FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests==2.32.3
COPY engine.py daily_summary.py config.json ./
COPY web ./web
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=10000
EXPOSE 10000
CMD ["python3","engine.py"]
