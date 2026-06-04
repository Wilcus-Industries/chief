# Google Drive MCP server (FastMCP, streamable-HTTP on :8001). Ported from genesis-x.
# Standalone image — imports nothing from the chief package. WeasyPrint needs the
# pango/cairo/gdk-pixbuf system libraries to render the upload-to-PDF tool.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
    libgdk-pixbuf-xlib-2.0-0 libffi-dev shared-mime-info \
    fonts-liberation \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

CMD ["python", "/app/server.py"]
