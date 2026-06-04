FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

WORKDIR /app

# Resume PDFs reference fonts the user picked in the structured editor. Base
# Playwright image only ships DejaVu/Liberation defaults — install the broader
# font set users expect (Liberation = metric-compatible Arial/Times/Courier;
# msttcorefonts = real Arial/Verdana/Tahoma family; Noto for emoji + extra
# scripts). Web fonts (Roboto/Inter/etc.) are loaded at render time via the
# Google Fonts <link> in resume_service._HTML_WRAPPER.
RUN apt-get update && \
    echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections && \
    apt-get install -y --no-install-recommends \
        fonts-liberation fonts-liberation2 \
        fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji \
        ttf-mscorefonts-installer && \
    rm -rf /var/lib/apt/lists/* && \
    fc-cache -fv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Base image ships Chromium for the image's pinned Playwright version. If the
# Python SDK from pip resolves to a newer release, that release expects a
# different browser build dir. Install whichever Chromium matches the SDK we
# actually have, so launch() finds it regardless of base-image alignment.
RUN playwright install chromium

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
