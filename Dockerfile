FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    git \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV CMAKE_POLICY_VERSION_MINIMUM=3.5

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python setup_lip_sync.py

RUN mkdir -p static/uploads instance

EXPOSE 5000

CMD ["gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:5000", "run:app"]
