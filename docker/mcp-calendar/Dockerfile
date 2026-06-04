# chief's own Google Calendar MCP server (FastMCP, streamable-HTTP on :8003).
# Standalone image — imports nothing from the chief package. Replaces the abandoned
# nspady mcp-gcal (its single shared HTTP transport 400'd on the 2nd connection; chief
# opens one MCP connection per task, so it died after the first task).
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
