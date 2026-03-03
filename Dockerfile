FROM python:3.11-slim

# All Chromium dependencies manually (avoids playwright install-deps which breaks on Debian Trixie)
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 \
    libglib2.0-0 libdbus-1-3 libexpat1 \
    fonts-liberation fonts-unifont \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium only (no install-deps — deps handled above)
RUN playwright install chromium

# Copy app files
COPY monitor.py bot.py ./

# Create empty events.json if not present
RUN echo '[]' > events.json

# Env vars (override at runtime)
ENV TG_TOKEN=8666314563:AAFXDLrKjlkWz41rLo9BLdkutJj4h1Y8JKA
ENV TG_CHAT_IDS=924367933,1707720927
ENV CHECK_INTERVAL=180

# Run both processes via a simple shell script
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
