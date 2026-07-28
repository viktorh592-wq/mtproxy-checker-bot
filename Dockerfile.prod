FROM python:3.11-slim

WORKDIR /app

# Копируем только requirements.txt сначала — для кэширования зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальные файлы
COPY bot.py .

# Запускаем
CMD ["python", "bot.py"]
