# Persona — headless / agent-ingest образ.
# ВНИМАНИЕ: в контейнере без дисплея НЕТ скриншот-захвата (mss/capture не
# работают без X/Windows). Этот образ — режим headless: веб-API + приём
# данных от внешних агентов. Фоновые воркеры отключены через PERSONA_LEAN_MODE=1.
FROM python:3.12-slim

# Системные зависимости. tesseract-ocr — опционально, нужен только если
# хочется OCR присланных извне картинок; без него OCR тихо отключён.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала только метаданные пакета — слой с зависимостями кешируется и не
# пересобирается при правке исходников.
COPY pyproject.toml README.md ./
COPY app ./app

# Ставим пакет «как есть» (editable), без тяжёлых extras (audio/embeddings).
RUN pip install --no-cache-dir -e .

# /data — единый корень БД, миниатюр, inbox, бэкапов (PERSONA_DATA_DIR).
# LEAN_MODE=1 — фоновые воркеры (захват/OCR/ретеншн) выключены: в headless
# им нечего делать, а SQLite-WAL не молотится впустую.
ENV PERSONA_DATA_DIR=/data \
    PERSONA_LEAN_MODE=1 \
    PERSONA_HOST=0.0.0.0 \
    PERSONA_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
VOLUME ["/data"]

# Фабрика приложения — uvicorn создаёт FastAPI через create_app().
CMD ["uvicorn", "app.web.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
