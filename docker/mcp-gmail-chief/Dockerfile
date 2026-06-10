# chief's own Google Gmail MCP server (FastMCP, streamable-HTTP). Standalone image —
# imports nothing from the chief package. Multi-account, full read+write surface
# (issues #48/#51/#52). After the issue #52 cutover this image backs the mcp-gmail
# compose service on :8004 (the third-party mcp-google-gmail wrapper was dropped).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py gmail_signature.py ./

CMD ["python", "/app/server.py"]
