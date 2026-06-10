# chief's browser automation MCP server. Runs the stock @playwright/mcp@0.0.76 package
# with headless Chromium over streamable HTTP on port 3000. No secrets — this container
# only controls a browser, it never touches the Max token or any credentials.
#
# Headless Chromium inside a container needs --no-sandbox (seccomp/apparmor restrictions
# on the host deny the kernel namespacing Chromium uses for its own sandboxing). The MCP
# package passes --no-sandbox through its own --headless Chromium launch.
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
# --port: bind the MCP HTTP server on 3000
# --host: bind on all interfaces so core can reach it over the compose network
CMD ["node", "node_modules/@playwright/mcp/cli.js", "--headless", \
     "--port", "3000", "--host", "0.0.0.0"]
