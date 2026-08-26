# dev-workflow ticket-loop agent image.
#
# A pinned, self-contained toolbox that runs ONE headless `/ticket-loop` pass. It
# bakes the runner + the plugin root-owned at /opt/dev-workflow (boundary rule 2);
# the target-repo checkout, ~/.claude auth, agent.env, and state.json all live in a
# persistent volume mounted at /home/agent — NOT in the image. So the auth you do
# once survives every ephemeral `docker run --rm`, and a `git pull` of the work tree
# can never change how the loop runs (only a rebuild can).
#
# Build context is the FRAMEWORK REPO ROOT (this repo), not this directory:
#   docker build -f skills/ticket-loop/docker/Dockerfile \
#     --build-arg CLAUDE_CODE_VERSION=<pin> -t <image>:<pin> <repo-root>
FROM node:22-bookworm-slim

# Pin the Claude Code CLI — NO default, so the caller MUST pin it (verify it against
# whatever `claude` your team runs before building). The autoupdater is disabled
# below so this stays put.
ARG CLAUDE_CODE_VERSION
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}

ENV DISABLE_AUTOUPDATER=1 \
    npm_config_update_notifier=false \
    PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# System toolchain. Adjust per target repo: git + gh (PRs), curl/ca-certs, jq,
# python3 + python3-yaml (dw-config.py reads dev-workflow.yml), tzdata (ships
# /usr/share/zoneinfo so the runner's `export TZ=<schedule.tz>` is effective —
# without it dates fall back to UTC and the daily-digest day gate misfires), and
# uv (Python deps for the quality gate). postgresql-client is here because many
# stacks' test suites reach a DB. build-essential + libpq-dev give gcc + pg_config
# + libpq headers so `uv sync` can compile source-only C-extension wheels (e.g.
# psycopg2==2.x, which ships no manylinux wheel) — without them a Django test gate
# can't even bootstrap its venv. python3-dev supplies Python.h, needed to compile
# any C extension against the system python. Trim or extend (a JS toolchain, wkhtmltopdf, …)
# to match YOUR repo's test dependencies.
RUN set -eux; \
    if [ -z "$CLAUDE_CODE_VERSION" ]; then \
      echo "ERROR: build-arg CLAUDE_CODE_VERSION is required (pin the claude version)"; exit 1; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      git curl ca-certificates gnupg jq python3 python3-yaml tzdata postgresql-client \
      build-essential libpq-dev python3-dev; \
    # GitHub CLI from its own apt repo (Debian doesn't package it)
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*; \
    # Claude Code CLI, pinned
    npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}; \
    # uv (to /usr/local/bin so it's on PATH for every user)
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# ── AWS CLI v2 + Session Manager plugin (SSM port-forward tunnels) ─────────────
# Analytics-style repos reach non-exposed databases via
# `aws ssm start-session` (AWS-StartPortForwardingSessionToRemoteHost), which
# needs BOTH binaries. Arch-aware: the nt box is arm64, CI runners are amd64.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      arm64) awsarch="aarch64"; smparch="ubuntu_arm64" ;; \
      amd64) awsarch="x86_64";  smparch="ubuntu_64bit" ;; \
      *) echo "ERROR: unsupported arch $arch"; exit 1 ;; \
    esac; \
    apt-get update; apt-get install -y --no-install-recommends unzip; \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${awsarch}.zip" -o /tmp/awscliv2.zip; \
    unzip -q /tmp/awscliv2.zip -d /tmp; \
    /tmp/aws/install; \
    curl -fsSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/${smparch}/session-manager-plugin.deb" \
      -o /tmp/session-manager-plugin.deb; \
    # node-slim's PATH lacks the sbin dirs dpkg's maintainer scripts need
    PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH" dpkg -i /tmp/session-manager-plugin.deb; \
    rm -rf /tmp/awscliv2.zip /tmp/aws /tmp/session-manager-plugin.deb /var/lib/apt/lists/*; \
    aws --version; session-manager-plugin --version

# ── Boundary rule 2: bake the runner + plugin root-owned at /opt/dev-workflow ──
# The mounted volume at /home/agent is the ONLY writable surface; /opt/dev-workflow
# is owned by root and the container runs as the non-root `agent` user, so a build
# subagent physically cannot edit the framework driving it.
COPY skills/ticket-loop/cron-run.sh \
     skills/ticket-loop/orchestrator/telegram-ingress.py \
     skills/ticket-loop/run-pass.sh \
     skills/ticket-loop/loop-lock.sh \
     skills/ticket-loop/telegram.py \
     skills/ticket-loop/usage-parse.py \
     skills/ticket-loop/usage-rollup.py \
     skills/ticket-loop/orchestrator/orchestrator.sh \
     skills/ticket-loop/orchestrator/orch.py \
     /opt/dev-workflow/bin/
COPY dev-workflow/dw-config.py dev-workflow/queue-count.py /opt/dev-workflow/bin/
# The plugin (for --plugin-dir): a copy of the plugin manifest + all skills.
COPY .claude-plugin/ /opt/dev-workflow/plugin/.claude-plugin/
COPY skills/ /opt/dev-workflow/plugin/skills/
# The keyed tracker MCP config the runner passes via TICKET_LOOP_MCP_CONFIG.
COPY skills/ticket-loop/docker/loop-mcp.json /opt/dev-workflow/loop-mcp.json
# PATH-visible Telegram bridge (same public CLI the --opt native install exposes).
RUN chmod -R 755 /opt/dev-workflow \
    && ln -sf /opt/dev-workflow/bin/telegram.py /usr/local/bin/dw-telegram

# Non-root agent user. Its home (/home/agent) is where the persistent volume mounts:
# ~/.claude (auth), the work-tree checkout, agent.env, state.json, .venv.
# Absolute path: the ENV PATH above deliberately omits /usr/sbin (the agent user
# never needs admin tools), so bare `useradd` is not found at build time.
RUN /usr/sbin/useradd --create-home --shell /bin/bash --uid 10001 agent
USER agent
WORKDIR /home/agent

# CMD (not ENTRYPOINT) so a `docker run <image> <cmd>` can override it — e.g. the
# box driver's `docker run <image> /opt/dev-workflow/bin/run-pass.sh --dry-run`.
# Default CMD stays the ONE-SHOT pass (the systemd timer shape runs the image
# with no command — swapping this would silently daemonize existing
# deployments). The multi-project orchestrator is started explicitly:
#   docker run … <image> /opt/dev-workflow/bin/orchestrator.sh
CMD ["/opt/dev-workflow/bin/run-pass.sh"]
