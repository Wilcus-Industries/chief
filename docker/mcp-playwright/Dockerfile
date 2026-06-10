# chief's browser automation MCP server. Runs the stock @playwright/mcp@0.0.76 package
# with headless Chromium over streamable HTTP on port 3000. No secrets — this container
# only controls a browser, it never touches the Max token or any credentials.
#
# Chromium inside a container needs --no-sandbox because the hardened compose profile
# (user 1000:1000, cap_drop ALL + SYS_ADMIN only, no-new-privileges:true) blocks the
# setuid chrome-sandbox helper that Chromium uses for its own process sandbox. The flag
# is passed explicitly on the CMD line; --headless does NOT imply it.
FROM mcr.microsoft.com/playwright:v1.52.0-jammy

# Install only the node runtime packages we need; playwright browsers are pre-installed
# in the base image. Use the LTS node from the base image (nodejs is already present).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin the exact @playwright/mcp version so the tool catalog stays stable.
RUN npm install --save-exact @playwright/mcp@0.0.76

# Expose the MCP streamable-HTTP port (never published to the host — only accessible
# on the compose internal network by core).
EXPOSE 3000

# --headless: headless Chromium (no display needed inside the container)
# --no-sandbox: required because the hardened profile blocks the setuid chrome-sandbox
#               helper (no-new-privileges:true + non-root user). --headless does NOT
#               imply --no-sandbox; both flags must be present.
# --port: bind the MCP HTTP server on 3000
# --host: bind on all interfaces so core can reach it over the compose network
# --user-data-dir: Chromium writes its full profile (cookies, localStorage, IndexedDB,
#               session state) directly to this path on every page visit.  Because the
#               path sits on the browser-profile named volume, the data outlives the
#               container — cookie-based logins survive restarts by construction, with
#               no explicit save step.  chief's browser session is single-instance (one
#               shared mcp-playwright container, serialised owner turns), so a single
#               persistent profile has no parallel-session collision risk.
CMD ["node", "node_modules/@playwright/mcp/cli.js", "--headless", "--no-sandbox", \
     "--port", "3000", "--host", "0.0.0.0", "--output-dir", "/screenshots", \
     "--user-data-dir", "/browser-profile"]
