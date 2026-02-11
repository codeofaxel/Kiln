FROM python:3.12-slim

WORKDIR /app

# Install kiln package
COPY . ./kiln/
RUN pip install --no-cache-dir ./kiln

# Create non-root user
RUN adduser --disabled-password --gecos '' kiln
USER kiln

# Default env vars (override at runtime)
ENV KILN_PRINTER_TYPE=octoprint
ENV KILN_PRINTER_HOST=""
ENV KILN_PRINTER_API_KEY=""

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["kiln"]
