FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Конфигурация через переменные окружения, без хардкода ключей.
# Примеры:
#   - API_KEY
#   - OPENAI_BASE_URL
ENV API_KEY="" \
    OPENAI_BASE_URL=""

# Порт по умолчанию берётся из server.py (OUROBOROS_SERVER_PORT или 8765).
ENV OUROBOROS_SERVER_PORT=8765
EXPOSE 8765

CMD ["python", "server.py"]

