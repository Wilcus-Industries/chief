# chief's shell/code sandbox (M7 + M10). A locked-down worker that runs the owner's bash
# commands with NO secrets, a restricted FS (the shared /workspace volume), and resource
# limits — core forwards each command here over a stdlib socket (no MCP/HTTP server).
#
# Build context is the repo root (see docker-compose) so we can copy the stdlib server
# file and the vendored skill tree out of the chief package.
#
# The stdlib shell server itself imports only the standard library. The office-doc skills
# (M10: docx/pdf/pptx/xlsx) are the one exception to the old "no dependencies" rule — they
# shell out to a Python + Node + pandoc + LibreOffice toolchain, installed below. This
# does NOT weaken the sandbox's security invariant: it stays secret-free (no `secrets:`
# block in compose), read-only rootfs, non-root, mem/PID/CPU-capped.
FROM python:3.12-slim

# Base utils + the office-doc toolchain. poppler-utils backs pdf2image; libreoffice
# (writer/calc/impress, pulls -core) backs office/soffice.py used by the docx convert,
# xlsx recalc, and pptx thumbnail paths; pandoc backs docx read/convert; nodejs+npm back
# the docx-js / pptxgenjs creation paths; fonts-liberation gives LibreOffice Office-
# fidelity rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils git curl ca-certificates \
    poppler-utils pandoc nodejs npm fonts-liberation \
    libreoffice-writer libreoffice-calc libreoffice-impress \
  && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /workspace

# Python deps for the office skills.
COPY docker/sandbox/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Node deps for the office skills, installed global so the read-only runtime rootfs never
# needs `npm install`. NODE_PATH lets the skills' `require('docx')` / `require('pptxgenjs')`
# resolve against the global tree.
RUN npm install -g docx pptxgenjs react react-dom react-icons sharp \
  && npm cache clean --force
ENV NODE_PATH=/usr/local/lib/node_modules

# The vendored skill scripts must live at the SAME absolute path core hands the model
# (core's WORKDIR is /app and it resolves os.path.abspath("vendor/chief-skills")), since
# the model's `python .../scripts/...` calls execute *here* in the sandbox, not in core.
COPY vendor/ /app/vendor/
# Fail loudly if the skills submodule wasn't checked out — guard the four office skills
# whose scripts actually run in the sandbox (see Dockerfile.core for the full plugin list).
RUN set -e; \
    for s in docx pdf pptx xlsx; do \
        test -d "/app/vendor/chief-skills/upstream/skills/$s" \
        || { echo "vendor/chief-skills/upstream/skills/$s missing — run 'git submodule update --init' before docker build" >&2; exit 1; }; \
    done

# A non-root user owns the workspace; compose also pins `user:` + no-new-privileges.
RUN useradd --create-home --uid 1000 sandbox

# The office toolchain (LibreOffice profile, npm/dconf caches) needs a writable HOME. The
# runtime rootfs is read-only and compose's /home/sandbox tmpfs mounts root-owned, so point
# HOME at the /tmp tmpfs (compose mounts it writable for uid 1000). Without this LibreOffice
# hangs creating its user profile.
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/.cache

# Just the stdlib shell server — run as a plain script, not as a package import.
COPY src/chief/sandbox/shell_server.py /app/shell_server.py

USER sandbox
CMD ["python", "/app/shell_server.py"]
