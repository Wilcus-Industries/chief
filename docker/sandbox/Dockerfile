# chief's shell/code sandbox (M7). A locked-down worker that runs the owner's bash
# commands with NO secrets, a restricted FS (the shared /workspace volume), and resource
# limits — core forwards each command here over a stdlib socket (no MCP/HTTP server).
#
# Build context is the repo root (see docker-compose) so we can copy the single stdlib
# server file out of the chief package. The image installs NO chief dependencies: the
# server imports only the standard library, keeping the sandbox surface minimal.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils git curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /workspace

# A non-root user owns the workspace; compose also pins `user:` + no-new-privileges.
RUN useradd --create-home --uid 1000 sandbox

# Just the stdlib shell server — run as a plain script, not as a package import.
COPY src/chief/sandbox/shell_server.py /app/shell_server.py

USER sandbox
CMD ["python", "/app/shell_server.py"]
