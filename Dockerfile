FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS base

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
