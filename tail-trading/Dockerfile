FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TAILTRADER_DB=/data/tailtrader.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tailtrader ./tailtrader
COPY config.yaml .
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "-m", "tailtrader"]
