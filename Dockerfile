FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./bot        /app/bot
COPY ./scripts    /app/scripts

RUN chmod +x /app/scripts/start.sh

EXPOSE 8000

CMD ["bash", "/app/scripts/start.sh"]
