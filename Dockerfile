FROM python:3.12-slim

# Install Chrome for headless publishing
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir playwright \
    && playwright install-deps chromium 2>/dev/null || true

COPY . .

# Default: launch Chrome headless, then run the loop
ENV HEADLESS=1 \
    CHROME_PATH=/usr/bin/google-chrome \
    DISPLAY=

CMD ["./entrypoint.sh"]