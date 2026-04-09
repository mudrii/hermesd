FROM python:3.11-slim AS base

LABEL maintainer="Nous Research"
LABEL description="TUI monitoring dashboard for Hermes AI agent"

RUN groupadd --gid 1000 hermesd \
    && useradd --uid 1000 --gid hermesd --create-home hermesd

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY hermesd/ hermesd/

RUN pip install --no-cache-dir . \
    && rm -rf /root/.cache

USER hermesd

# Mount ~/.hermes as a volume at runtime:
#   docker run -it -v ~/.hermes:/home/hermesd/.hermes:ro hermesd
ENV HERMES_HOME=/home/hermesd/.hermes

ENTRYPOINT ["hermesd"]
