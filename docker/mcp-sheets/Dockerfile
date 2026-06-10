# Google Sheets MCP server (FastMCP, streamable-HTTP on :8002).
# First-party implementation replacing xing5/mcp-google-sheets (issue #47):
# multi-account per-request credential selection + atomic per-account
# token write-back.  Row-1 guard preserved from the original wrapper.
# Standalone image — imports nothing from the chief package.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py row1_guard.py ./

CMD ["python", "/app/server.py"]
