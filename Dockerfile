FROM python:3.11-slim

WORKDIR /app

# Evita archivos .pyc y fuerza logs en tiempo real en Coolify
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Carpeta donde vivirá la base SQLite persistente
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]