FROM python:3.13-slim

# System deps: tesseract, ghostscript, chromium deps, Xvfb
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    ghostscript \
    xvfb \
    chromium \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers
RUN playwright install chromium

# Copy app
COPY . .

# Create needed dirs
RUN mkdir -p uploads originales chrome_profile extension

# Expose
EXPOSE 8000

# Start with Xvfb for Playwright
CMD ["python3", "-c", \
     "import subprocess, os, time, signal, sys; \
      xvfb = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x1024x24'], \
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); \
      os.environ['DISPLAY'] = ':99'; \
      time.sleep(1); \
      proc = subprocess.Popen(['python3', '-m', 'uvicorn', 'app:app', '--host', '0.0.0.0', '--port', '8000']); \
      def handle(sig, frame): proc.terminate(); xvfb.terminate(); sys.exit(0); \
      signal.signal(signal.SIGTERM, handle); signal.signal(signal.SIGINT, handle); \
      proc.wait()"]
