# One image for all four ShopSense services (gateway, orders, inventory, agents).
# docker-compose.yml starts it four times with a different `command`.
#
# No secrets and no data are baked in: the Gemini key is passed at run time by docker-compose.yml.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as a normal user, not root.
RUN useradd --create-home --uid 1000 shopsense

# Dependencies first, so editing code does not re-install them (Docker caches this layer).
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY services ./services
COPY ui ./ui
COPY evals ./evals
COPY scripts ./scripts

# The SQLite files are created here at start-up (services/common/db.py). They vanish with the container.
RUN mkdir -p /app/data && chown -R shopsense:shopsense /app
USER shopsense

# Default command = the gateway. docker-compose.yml overrides it for the other services.
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "services.gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
