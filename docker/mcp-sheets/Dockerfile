# Google Sheets MCP server (FastMCP, streamable-HTTP on :8002). Wraps the third-party
# xing5/mcp-google-sheets package with a server-side row-1 (header) write guard.
# Standalone image — imports nothing from the chief package.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

CMD ["python", "/app/server.py"]
