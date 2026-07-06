FROM python:3.12-slim

ARG HOST_PROXY=http://172.17.0.1:3128

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    http_proxy=${HOST_PROXY} \
    https_proxy=${HOST_PROXY} \
    HTTP_PROXY=${HOST_PROXY} \
    HTTPS_PROXY=${HOST_PROXY}

WORKDIR /app
USER 0
RUN printf \
        'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
        "$http_proxy" \
        "$https_proxy" \
        > /etc/apt/apt.conf.d/99proxy \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY input ./input

CMD ["python", "-m", "src.main", "--watch-input", "--headless", "--selenium-remote-url"]