FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates && \
    ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then DENO_ARCH="aarch64"; else DENO_ARCH="x86_64"; fi && \
    curl -fsSL "https://github.com/denoland/deno/releases/download/v2.9.6/deno-${DENO_ARCH}-unknown-linux-gnu.zip" -o /tmp/deno.zip && \
    unzip -o /tmp/deno.zip -d /usr/local/bin && \
    chmod +x /usr/local/bin/deno && \
    rm -f /tmp/deno.zip && \
    apt-get purge -y unzip && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

CMD ["python", "bot.py"]
