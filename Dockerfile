FROM python:3.12-slim

WORKDIR /app

# Install kiln package
COPY kiln/ ./kiln/
# The skill file lives at the repository root; kiln/setup.py copies it into
# the package from one level up at build time.
COPY SKILL.md ./SKILL.md
RUN pip install --no-cache-dir ./kiln

# Printer connection (override at runtime with -e)
ENV KILN_PRINTER_TYPE=octoprint
ENV KILN_PRINTER_HOST=""
ENV KILN_PRINTER_API_KEY=""

# `kiln serve` speaks MCP over stdio (no network port). Unattended deploys
# (Glama Release, Docker MCP catalog, CI) must accept the Terms of Use for the
# server to boot non-interactively — set this in the DEPLOY environment so
# terms acceptance stays an explicit operator choice, not baked into the image:
#   docker run -i -e KILN_ACCEPT_TERMS=1 <image>
CMD ["kiln", "serve"]
