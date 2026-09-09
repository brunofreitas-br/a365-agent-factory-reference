FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Não roda como root: o console é a única superfície pública da fábrica.
RUN useradd --create-home --uid 10001 console
USER console

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
