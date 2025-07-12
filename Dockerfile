# Используем официальный легковесный образ Python 3.11
FROM python:3.11-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Копируем файл зависимостей в контейнер
COPY requirements.txt .

# Устанавливаем зависимости из requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код проекта в контейнер
COPY . .

# Переменная окружения для токена (можно переопределить при запуске контейнера)
ENV API_TOKEN=""

# Команда запуска бота
CMD ["python", "spy_bot.py"]
