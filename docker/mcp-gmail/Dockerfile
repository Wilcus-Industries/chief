# Google Gmail MCP server (FastMCP, streamable-HTTP on :8004). Wraps the third-party
# MindMadeLab/mcp-google-gmail package with a server-side "sent by an assistant"
# signature guard. Standalone image — imports nothing from the chief package.
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
