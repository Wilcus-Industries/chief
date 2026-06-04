# Vendored build of nspady/google-calendar-mcp v2.6.2 with a one-line patch.
#
# Why vendor: in HTTP mode the upstream server builds ONE StreamableHTTPServerTransport
# in stateless mode at boot and reuses it for every request. @modelcontextprotocol/sdk
# 1.27.1 (the version nspady's lockfile pins) added a guard that throws on the 2nd+
# handleRequest of a *stateless* transport, so the CLI's `initialize` succeeds but the
# follow-up `tools/list` 500s and the session dies with zero tools. stateful-transport.patch
# gives the shared transport a sessionIdGenerator (stateful mode), which the guard allows.
# Upstream is unfixed (v2.6.2 is the latest tag) and exposes no config knob, hence the patch.
#
# Build steps mirror upstream's own Dockerfile (node:18-alpine, npm ci -> build -> prune).
FROM node:18-alpine
RUN apk add --no-cache git
WORKDIR /app
RUN git clone --depth 1 --branch v2.6.2 https://github.com/nspady/google-calendar-mcp.git .
COPY stateful-transport.patch .
RUN git apply stateful-transport.patch
RUN npm ci --no-audit --no-fund --silent \
    && npm run build \
    && npm prune --production --silent
# nspady persists refreshed tokens under its config dir; create it for the default user.
# (compose overrides USER via `user:` to keep the token bind-mount writable.)
RUN mkdir -p /home/nodejs/.config/google-calendar-mcp
EXPOSE 3000
CMD ["node", "build/index.js"]
