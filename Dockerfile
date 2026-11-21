FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала зависимости — так Docker кэширует слой и не переустанавливает их при каждом изменении кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY spy_bot.py .

# Не запускаем бота от root
RUN useradd --create-home bot
USER bot

# API_TOKEN передаётся при запуске: docker run -e API_TOKEN=... spybot
CMD ["python", "spy_bot.py"]
