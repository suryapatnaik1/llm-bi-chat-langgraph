FROM python:3.11-slim

# Install Poetry
RUN pip install --no-cache-dir poetry==1.8.5

WORKDIR /app

# Install dependencies first (layer cache)
COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-ansi

# Copy application code
COPY scripts/ scripts/
COPY src/ src/
COPY .streamlit/ .streamlit/

# Create a non-root user and hand ownership of the app directory to it.
# Running as root inside a container is a HIGH-severity misconfiguration (DS-0002):
# if the container is compromised, root inside maps to root on the host.
RUN useradd --no-create-home --shell /bin/false appuser \
    && mkdir -p local_data/uploads local_data/reports src/static/reports \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

CMD ["streamlit", "run", "src/app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]
