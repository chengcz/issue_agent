FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://github.com/cli/cli/releases/download/v2.79.0/gh_2.79.0_linux_amd64.tar.gz \
       | tar -xz --strip-components=2 -C /usr/local/bin gh_2.79.0_linux_amd64/bin/gh

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /data/state /data/worktrees /workspace \
    && chown -R agent:agent /data /workspace
USER agent
ENTRYPOINT ["cao"]
CMD ["--config", "/config/orchestrator.toml", "serve"]
