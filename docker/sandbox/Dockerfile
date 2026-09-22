# Jiffy Generic Sandbox Image
#
# One generic image for all tasks — includes version managers (nvm, uv, gvm)
# for Node.js/Python/Go, git provider CLIs (gh, glab, tea), git, curl,
# build-essential, iptables (for egress restriction), and the OpenCode coding
# agent CLI.
#
# The agent is responsible for installing specific language/runtime versions
# at runtime via the version managers, and for any additional dependencies.
#
# Network egress restriction (enabled by default via JIFFY_SANDBOX_NETWORK_RESTRICTED)
# is applied at container start by the Gateway using iptables inside the
# container's own network namespace; see docker/sandbox/README.md.
#
# Build: docker build -t jiffy-sandbox:1.3.0 .
# The image tag used at runtime is read from SANDBOX_IMAGE env var.

FROM debian:bookworm-20260713

ARG DEBIAN_FRONTEND=noninteractive

# Pinned tool versions
ARG NVM_VERSION=0.40.6
ARG GH_VERSION=2.72.0
ARG GLAB_VERSION=1.109.0
ARG TEA_VERSION=0.9.0
ARG NODE_VERSION=24
ARG PYTHON_VERSION=3.12
ARG GO_VERSION=1.26.5

# 1. Base dependencies (kept from original image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ca-certificates \
    gnupg \
    iptables \
    && mkdir -p /etc/apt/keyrings \
    && rm -rf /var/lib/apt/lists/*

# 2. Version managers — Node.js (nvm), Python (uv), Go (gvm)
#    The agent installs specific language versions at runtime via these managers.

# --- nvm (Node Version Manager) ---
ENV NVM_DIR=/usr/local/nvm
RUN mkdir -p "$NVM_DIR" \
    && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh | bash

# Make nvm available in non-interactive shells via /etc/profile.d
RUN printf 'export NVM_DIR="%s"\n[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\n' "$NVM_DIR" \
    > /etc/profile.d/nvm.sh

# --- Node.js via nvm ---
RUN bash -c '. /etc/profile.d/nvm.sh \
    && nvm install ${NODE_VERSION} \
    && nvm alias default ${NODE_VERSION} \
    && NODE_BIN_DIR="$(dirname "$(nvm which ${NODE_VERSION})")" \
    && ln -sf "$NODE_BIN_DIR/node" /usr/local/bin/node \
    && ln -sf "$NODE_BIN_DIR/npm" /usr/local/bin/npm \
    && ln -sf "$NODE_BIN_DIR/npx" /usr/local/bin/npx'

RUN node --version && npm --version

# --- pnpm via corepack (falls back to npm if corepack prepare fails) ---
# Installed at build time (as root) so the non-root user has access without
# permission errors. corepack is bundled with Node.js >= 16.10 / npm >= 8.3.
# /usr/local/bin/pnpm is a symlink to a self-contained script (shebang
# `#!/usr/bin/env node`), so it keeps working even if the agent later runs
# `nvm install`/`nvm use` to switch the active Node.js version at runtime —
# `env node` simply resolves to whichever node is on PATH at invocation time.
RUN bash -c '. /etc/profile.d/nvm.sh \
    && NODE_BIN_DIR="$(dirname "$(nvm which ${NODE_VERSION})")" \
    && corepack enable \
    && (corepack prepare pnpm@latest --activate || npm install -g pnpm) \
    && ln -sf "$NODE_BIN_DIR/pnpm" /usr/local/bin/pnpm \
    && (ln -sf "$NODE_BIN_DIR/pnpx" /usr/local/bin/pnpx || true) \
    && pnpm --version'

# --- uv (Python package/version manager) ---
# Installed to $HOME/.local/bin by default; add to PATH explicitly.
# UV_PYTHON_INSTALL_DIR directs Python installs to a system-wide location
# accessible to all users, not under /root which is restricted at runtime.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}" \
    UV_PYTHON_INSTALL_DIR=/usr/local/uv-python
RUN uv --version \
    && uv python install ${PYTHON_VERSION} \
    && echo "Python binary at: $(uv python find ${PYTHON_VERSION})" \
    && ln -sf "$(uv python find ${PYTHON_VERSION})" /usr/local/bin/python3 \
    && ln -sf /usr/local/bin/python3 /usr/local/bin/python \
    && echo "=== Python install verification ===" \
    && python3 --version \
    && echo "=== Python OK ==="

# --- gvm (Go Version Manager) ---
# The official installer clones the repo AND generates scripts/gvm from scripts/gvm.in
ENV GVM_DIR=/usr/local/gvm
RUN curl -fsSL https://raw.githubusercontent.com/moovweb/gvm/master/binscripts/gvm-installer \
    | GVM_DIR="$GVM_DIR" bash || echo "gvm install failed (network issue) — continuing"

# Make gvm available in non-interactive shells via /etc/profile.d
RUN printf 'export GVM_DIR="%s"\n[ -s "$GVM_DIR/scripts/gvm" ] && . "$GVM_DIR/scripts/gvm"\n' "$GVM_DIR" \
    > /etc/profile.d/gvm.sh

# --- Go via direct download ---
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tar.gz \
    && rm -rf /usr/local/go \
    && tar -C /usr/local -xzf /tmp/go.tar.gz \
    && rm /tmp/go.tar.gz \
    && ln -sf /usr/local/go/bin/go /usr/local/bin/go \
    && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt

RUN go version

# 3. Git provider CLIs — pinned versions

# --- gh (GitHub CLI) ---
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar xz --strip-components=2 -C /usr/local/bin "gh_${GH_VERSION}_linux_amd64/bin/gh"

# --- glab (GitLab CLI) ---
RUN curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.tar.gz" \
    | tar xz --strip-components=1 -C /usr/local/bin "bin/glab"

# --- tea (Gitea CLI) ---
#RUN curl -fsSL "https://gitea.com/gitea/tea/releases/download/v${TEA_VERSION}/tea_${TEA_VERSION}_linux_amd64.tar.gz" \
#    | tar xz -C /usr/local/bin "tea"

# 4. Coding agent CLI: OpenCode
#    Runs in non-interactive mode from /workspace.
#    OpenCode auto-discovers AGENTS.md (or falls back to CLAUDE.md) from CWD.
#    OPENCODE_DISABLE_CLAUDE_CODE is intentionally NOT set so the fallback works.
#
#    The version is pinned. Unpinned, every rebuild silently adopts whatever
#    is current, so the agent's runtime changes without a commit — 1.18.31
#    started rejecting OpenCode Zen's free models with a 403 FreeTierError
#    that reaches the Gateway only as an opaque "UnknownError", while 1.18.30
#    serves them fine. Bump this deliberately, and bump the image tag with it
#    so ensure_sandbox_image() actually rebuilds.
ARG OPENCODE_VERSION=1.18.30
RUN curl -fsSL https://opencode.ai/install | bash -s -- --version "${OPENCODE_VERSION}"

# Find where opencode was installed and copy to /usr/local/bin
# Install script adds to PATH in .bashrc, so we source it and use which
RUN OPENCODE_PATH=$(bash -c 'source /root/.bashrc 2>/dev/null; which opencode' 2>/dev/null || true) \
    && if [ -z "$OPENCODE_PATH" ]; then \
         OPENCODE_PATH=$(find /root -name opencode -type f 2>/dev/null | head -1); \
       fi \
    && if [ -z "$OPENCODE_PATH" ]; then \
         OPENCODE_PATH=$(find /usr/local -name opencode -type f 2>/dev/null | head -1); \
       fi \
    && if [ -n "$OPENCODE_PATH" ]; then \
         cp "$OPENCODE_PATH" /usr/local/bin/opencode; \
         echo "Copied opencode from $OPENCODE_PATH to /usr/local/bin"; \
       else \
         echo "ERROR: opencode binary not found after install" >&2; \
         exit 1; \
       fi

# 5. Non-root user
RUN useradd -ms /bin/bash jiffy \
    && mkdir -p /workspace && chown jiffy:jiffy /workspace

# Give jiffy a .bashrc and .profile that source version managers
# .bashrc — for interactive non-login shells (e.g. `docker exec -it ... bash`)
RUN printf '#!/bin/bash\n\
# Source version managers for interactive shells\n\
[ -s /etc/profile.d/nvm.sh ] && . /etc/profile.d/nvm.sh\n\
export PATH="/usr/local/bin:$PATH"\n\
' > /home/jiffy/.bashrc \
    && chmod +x /home/jiffy/.bashrc \
    && chown jiffy:jiffy /home/jiffy/.bashrc

# .profile — for login shells (e.g. `docker exec ... bash -l`)
RUN printf '#!/bin/bash\n\
# Source version managers for login shells\n\
[ -s /etc/profile.d/nvm.sh ] && . /etc/profile.d/nvm.sh\n\
export PATH="/usr/local/bin:$PATH"\n\
' > /home/jiffy/.profile \
    && chmod +x /home/jiffy/.profile \
    && chown jiffy:jiffy /home/jiffy/.profile

# --- npm/pnpm network resilience ---
# The sandbox egress to the npm registry can be slow/throttled. pnpm's default
# fetch-timeout is 60s; large tarball downloads (e.g. next, sharp) can exceed it,
# which aborts the request with a DOMException TIMEOUT_ERR ("error (23)") and
# retries/retries-out. pnpm v11 only reads network settings like these from its
# own global config file (~/.config/pnpm/config.yaml), NOT from ~/.npmrc, so
# raise the timeout and reduce concurrency there so installs complete reliably
# on a slow link.
# dangerouslyAllowAllBuilds restores the classic behavior of running postinstall
# scripts (needed by husky, esbuild, sharp, etc.) inside the trusted sandbox.
RUN mkdir -p /home/jiffy/.config/pnpm \
    && printf 'fetchTimeout: 600000\n\
fetchRetries: 3\n\
fetchRetryMintimeout: 10000\n\
fetchRetryMaxtimeout: 60000\n\
networkConcurrency: 6\n\
dangerouslyAllowAllBuilds: true\n\
' > /home/jiffy/.config/pnpm/config.yaml \
    && chown -R jiffy:jiffy /home/jiffy/.config

# 6. OpenCode config placeholder
#    Creates a minimal default config at the standard OpenCode global config path.
#    This file is expected to be overridden at runtime via a volume mount:
#      docker run -v /host/path/to/opencode.json:/home/jiffy/.config/opencode/opencode.json:ro ...
#    The placeholder ensures OpenCode works even if no external config is mounted.
RUN mkdir -p /home/jiffy/.config/opencode \
    && echo '{"$schema": "https://opencode.ai/config.json"}' > /home/jiffy/.config/opencode/opencode.json \
    && chown -R jiffy:jiffy /home/jiffy/.config

# 7. Verify language runtimes are globally available in a non-interactive shell
RUN echo "=== Language Runtime Verification ===" \
    && python3 --version \
    && node --version \
    && npm --version \
    && pnpm --version \
    && go version \
    && echo "=== All runtimes verified ==="

# 8. Startup report script — runs on every container start
COPY startup-report.sh /usr/local/bin/startup-report.sh
RUN chmod +x /usr/local/bin/startup-report.sh

USER jiffy
WORKDIR /workspace

# Print startup report on container start, then keep alive for exec_run.
CMD ["bash", "-c", "/usr/local/bin/startup-report.sh; while true; do sleep 86400; done"]
