FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY config.yaml .
ENV BOT_STATE=/data/state.json PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "bot", "daemon"]
