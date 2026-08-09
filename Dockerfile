FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
COPY rest_service ./rest_service
COPY mcp_server ./mcp_server
COPY scripts ./scripts

ENV PYTHONPATH=/app

CMD ["uvicorn", "rest_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
