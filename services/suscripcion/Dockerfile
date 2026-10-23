FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Primer despliegue en staging (HU15): la imagen se publica con el comentario cicd/deploy-staging.
COPY app ./app
COPY reglas ./reglas
# Migraciones del esquema suscripcion: alembic upgrade head (ver docs/adr/0003)
COPY alembic.ini ./
COPY migrations ./migrations

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
