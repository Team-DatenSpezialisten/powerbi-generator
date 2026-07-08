FROM python:3.12-slim

WORKDIR /app

# Abhängigkeiten zuerst (bessere Layer-Caches)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# App Runner / Container-Port
EXPOSE 8000

# Produktionsstart (ohne --reload)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
