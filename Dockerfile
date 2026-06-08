FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN uv pip install --system .

# No HEALTHCHECK: this is a stdio MCP server, not an HTTP service — there is
# no port/endpoint to probe. Use the `health_check` MCP tool for liveness.

ENTRYPOINT ["mcp-duplicati"]
