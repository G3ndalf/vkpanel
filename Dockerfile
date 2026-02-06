# VK IP Panel Dockerfile
FROM python:3.11-slim

# Метаданные
LABEL maintainer="G3ndalf"
LABEL description="VK IP Panel — панель управления скриптами ловли floating IP"

# Рабочая директория
WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements первыми для кэширования слоя
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

# Создаём директорию для данных
RUN mkdir -p /opt/vkpanel

# Переменные окружения по умолчанию
ENV DATA_FILE=/opt/vkpanel/data.json
ENV PYTHONUNBUFFERED=1

# Порт
EXPOSE 8000

# Запуск
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
