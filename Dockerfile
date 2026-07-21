FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV LSI_DATA_DIR=/app/data \
    STORCLI_PATH=/app/storcli64 \
    FLASK_RUN_HOST=0.0.0.0 \
    FLASK_RUN_PORT=5200

EXPOSE 5200

CMD ["python3", "web/app.py"]
