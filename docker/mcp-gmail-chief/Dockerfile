# chief's own Google Gmail MCP server (FastMCP, streamable-HTTP on :8005).
# Standalone image — imports nothing from the chief package. Multi-account read slice
# (issue #48). Runs alongside mcp-gmail (third-party) until cutover in issue #52.
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
