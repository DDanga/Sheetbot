# Sheetbot in a container: the code lives in the image, the settings and the data
# stay in the bot folder on your computer (mounted as /config, see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PYTHONPATH=/app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
COPY sheetbot ./sheetbot

# .env, the Google key and data/ are read from here, like when running without Docker
WORKDIR /config
# -P: take the code from /app, not from a "sheetbot" folder that happens to be in /config
CMD ["python", "-P", "-m", "sheetbot"]
