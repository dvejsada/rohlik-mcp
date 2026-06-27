# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build a self-contained virtualenv with the package and its dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY rohlik_mcp ./rohlik_mcp
RUN pip install --upgrade pip && pip install .


FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ROHLIK_MCP_HOST=0.0.0.0 \
    ROHLIK_MCP_PORT=8000 \
    ROHLIK_MCP_PATH=/mcp/

# Copy the prebuilt virtualenv from the build stage
COPY --from=build /opt/venv /opt/venv

# Run as an unprivileged user
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000

# Streamable HTTP endpoint is served at http://<host>:8000/mcp/
CMD ["rohlik-mcp"]
