FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS base

LABEL maintainer="Nous Research"
LABEL description="TUI monitoring dashboard for Hermes AI agent"

RUN groupadd --gid 1000 hermesd \
    && useradd --uid 1000 --gid hermesd --create-home hermesd

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN pip install --no-cache-dir pip==26.2.1 uv==0.12.10 \
    && uv sync --locked --no-dev --no-install-project

COPY hermesd/ hermesd/
RUN uv sync --locked --no-dev \
    && rm -rf /root/.cache /root/.cache/uv

USER hermesd

# Mount ~/.hermes as a volume at runtime:
#   docker run -it -v ~/.hermes:/home/hermesd/.hermes:ro hermesd
ENV HERMES_HOME=/home/hermesd/.hermes
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["hermesd"]
