FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Migraciones del esquema socios: alembic upgrade head (ver docs/adr/0003)
COPY alembic.ini ./
COPY migrations ./migrations
# Siembra del socio de prueba: la ejecuta el Job `seed_command` de services.yaml (HU32).
COPY scripts/sembrar_socio_demo.py ./scripts/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
