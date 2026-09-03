FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# Keep application code outside /app. Bothost uses /app/data for persistent data
# and may bind-mount /app at runtime.
WORKDIR /usr/src/xzona

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 8080

CMD ["python", "main.py"]
