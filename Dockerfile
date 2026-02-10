FROM python:3.12-slim

WORKDIR /app

# Install kiln package
COPY kiln/ ./kiln/
RUN pip install --no-cache-dir ./kiln

# Default env vars (override at runtime)
ENV KILN_PRINTER_TYPE=octoprint
ENV KILN_PRINTER_HOST=""
ENV KILN_PRINTER_API_KEY=""

EXPOSE 8000

CMD ["kiln"]
