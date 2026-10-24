# Blind benchmark target — intentionally vulnerable, never deploy for real.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# iputils-ping so the (planted) command-injection endpoint has a real `ping`
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
EXPOSE 5002
CMD ["python", "app.py"]
