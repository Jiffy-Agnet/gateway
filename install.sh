#!/usr/bin/env bash
#
# install.sh — one-command installer for Jiffy Gateway
#
# Usage (remote, recommended):
#   curl -fsSL https://raw.githubusercontent.com/Jiffy-Agnet/gateway/develop/install.sh | bash
#
# Usage (local, after cloning):
#   ./install.sh
#
# What it does:
#   1. Installs missing prerequisites (git, curl, Docker + Compose plugin).
#   2. Ensures the host has enough swap (JIFFY_SWAP_RAM_MULTIPLIER x RAM) so
#      sandbox containers don't get OOM-killed during heavy installs.
#   3. Clones (or updates) the Jiffy Gateway source.
#   4. Creates .env from .env.example and auto-generates every secret it
#      safely can (SECRET_KEY, REDIS_PASSWORD, per-provider ingest tokens).
#   5. If anything is left that genuinely needs a human decision (a real
#      external credential that can't be guessed), it stops, tells you
#      exactly which .env key(s) to fill in, and asks you to re-run the
#      same command — no interactive prompts, since those aren't reliable
#      when this script is piped into bash.
#   6. Builds and starts Redis, the Docker Socket Proxy, the web service,
#      and the Celery worker from compose.prod.yml, then runs
#      database migrations.
#
# Safe to re-run at any point: every step checks the current state first
# and only does what's still needed.
#
# Every step is logged to $LOG_FILE — a plain file in the directory you
# ran this script from (default: ./install.log). No extra folder is ever
# created for it.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (override by exporting these before running the script)
# ---------------------------------------------------------------------------
JIFFY_REPO_URL="${JIFFY_REPO_URL:-https://github.com/Jiffy-Agnet/gateway.git}"
JIFFY_REPO_BRANCH="${JIFFY_REPO_BRANCH:-develop}"
JIFFY_INSTALL_DIR="${JIFFY_INSTALL_DIR:-$HOME/jiffy-gateway}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yml}"

# Host swap sizing. The install script makes sure total swap reaches at least
# JIFFY_SWAP_RAM_MULTIPLIER x total RAM before Jiffy starts running tasks, so
# sandbox containers don't get OOM-killed on resource-constrained hosts.
JIFFY_SWAP_RAM_MULTIPLIER="${JIFFY_SWAP_RAM_MULTIPLIER:-2}"
JIFFY_SWAP_FILE="${JIFFY_SWAP_FILE:-/var/jiffy-swapfile}"

# Plain file in the directory the script was invoked from. Resolved once,
# right here, before anything cd's elsewhere — no directories are created
# for it.
LOG_FILE="${JIFFY_INSTALL_LOG:-$(pwd)/install.log}"

# Services actually started by this script. "sandbox" in the compose file is
# a manual smoke-test/debug container, not part of the task-execution path —
# real per-task sandboxes are provisioned on demand by celery through the
# docker-socket-proxy — so it's intentionally left out here.
COMPOSE_SERVICES=(redis docker-socket-proxy web celery)

# .env keys that are genuinely optional and should never block installation
# even if left blank.
OPTIONAL_KEYS=(SENTRY_DSN SANDBOX_NETWORK_ALLOWLIST SANDBOX_NETWORK_ALLOWLIST_EXTRA DOCKER_HOST REDIS_PORT
  SANDBOX_MEM_LIMIT SANDBOX_MEMORY_LIMIT SANDBOX_MEMORY_SWAP_LIMIT SANDBOX_CPU_LIMIT
  SANDBOX_NODE_MAX_OLD_SPACE_MB SANDBOX_PACKAGE_CONCURRENCY CELERY_WORKER_CONCURRENCY)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
if ! touch "$LOG_FILE" 2>/dev/null; then
  LOG_FILE="/tmp/jiffy-install.log"
  touch "$LOG_FILE"
fi

log() {
  local level="$1"; shift
  printf '[%s] [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$level" "$*" | tee -a "$LOG_FILE" >&2
}
info() { log "INFO" "$@"; }
warn() { log "WARN" "$@"; }
fail() {
  log "ERROR" "$@"
  echo
  echo "Installation failed. Full log: $LOG_FILE" >&2
  exit 1
}

trap 'fail "Unexpected error on line $LINENO. See $LOG_FILE for details."' ERR

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif have sudo; then
    sudo "$@"
  else
    fail "This step needs root privileges and 'sudo' is not available: $*"
  fi
}

random_secret() {
  if have openssl; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# ---------------------------------------------------------------------------
# Step 1 — git, curl
# ---------------------------------------------------------------------------
install_basic_tools() {
  info "Checking for git and curl..."
  local missing=()
  have git  || missing+=(git)
  have curl || missing+=(curl)

  if [ "${#missing[@]}" -eq 0 ]; then
    info "git and curl already present."
    return
  fi

  info "Installing missing tools: ${missing[*]}"
  if have apt-get; then
    as_root apt-get update -y >>"$LOG_FILE" 2>&1
    as_root apt-get install -y "${missing[@]}" >>"$LOG_FILE" 2>&1
  elif have dnf; then
    as_root dnf install -y "${missing[@]}" >>"$LOG_FILE" 2>&1
  elif have yum; then
    as_root yum install -y "${missing[@]}" >>"$LOG_FILE" 2>&1
  elif have pacman; then
    as_root pacman -Sy --noconfirm "${missing[@]}" >>"$LOG_FILE" 2>&1
  elif have apk; then
    as_root apk add --no-cache "${missing[@]}" >>"$LOG_FILE" 2>&1
  else
    fail "No supported package manager found (apt/dnf/yum/pacman/apk). Install manually: ${missing[*]}"
  fi
}

# ---------------------------------------------------------------------------
# Step 2 — Swap size check & adjustment
# ---------------------------------------------------------------------------
# Resource-constrained self-hosted servers (e.g. 2 CPU / 4 GB RAM) shared with
# other services often have little or no swap, which lets sandbox containers
# get OOM-killed (exit 137) during heavy operations like package installs.
# This step proactively makes sure total swap reaches JIFFY_SWAP_RAM_MULTIPLIER
# x total RAM. Best-effort: any failure (not enough disk, missing tools, no
# root) logs a warning and skips — a failed swap step never aborts the install.
# Idempotent: an existing Jiffy swap file is grown (not duplicated) and
# /etc/fstab never gets a duplicate entry.
ensure_swap() {
  info "Checking host swap..."
  if [ ! -f /proc/meminfo ]; then
    warn "No /proc/meminfo found — skipping swap check."
    return 0
  fi

  local mem_kb swap_kb multiplier target_kb
  mem_kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
  swap_kb="$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)"
  multiplier="${JIFFY_SWAP_RAM_MULTIPLIER:-2}"
  case "$multiplier" in
    ''|*[!0-9.]*)
      warn "JIFFY_SWAP_RAM_MULTIPLIER='$multiplier' is not a positive number, using 2."
      multiplier=2 ;;
  esac
  target_kb="$(awk -v m="$multiplier" -v k="$mem_kb" 'BEGIN { printf "%d", m*k }')"

  info "Detected RAM: $((mem_kb / 1024)) MB, existing swap: $((swap_kb / 1024)) MB, target swap (${multiplier}x RAM): $((target_kb / 1024)) MB."

  if [ "$swap_kb" -ge "$target_kb" ]; then
    info "Swap already sufficient ($((swap_kb / 1024)) MB >= $((target_kb / 1024)) MB) — nothing to do."
    return 0
  fi

  local swap_file dir file_kb active_kb non_managed_kb needed_kb avail_kb
  swap_file="${JIFFY_SWAP_FILE}"
  dir="$(dirname "$swap_file")"

  file_kb=0
  if [ -f "$swap_file" ]; then
    file_kb="$(( $(stat -c %s "$swap_file" 2>/dev/null || echo 0) / 1024 ))"
  fi
  active_kb="$(awk -v f="$swap_file" '$1==f{print $3}' /proc/swaps 2>/dev/null)"
  [ -n "$active_kb" ] || active_kb=0

  # How much our managed file must provide (non-managed swap already counts).
  non_managed_kb="$(( swap_kb - active_kb ))"
  needed_kb="$(( target_kb - non_managed_kb ))"

  if [ "$file_kb" -lt "$needed_kb" ]; then
    avail_kb="$(df -k "$dir" 2>/dev/null | awk 'NR==2{print $4}')"
    if [ -z "$avail_kb" ] || [ "$avail_kb" -lt "$needed_kb" ]; then
      warn "Only ${avail_kb:-unknown} KB free on $dir but ${needed_kb} KB of swap is needed — skipping swap setup. Install will continue without it."
      return 0
    fi

    if [ "$active_kb" -gt 0 ]; then
      info "Disabling existing swap file $swap_file to resize it..."
      as_root swapoff "$swap_file" >>"$LOG_FILE" 2>&1 || { warn "Could not disable $swap_file — skipping swap setup."; return 0; }
      active_kb=0
    fi

    if [ -f "$swap_file" ]; then
      info "Extending $swap_file to $((needed_kb / 1024)) MB..."
    else
      info "Creating $swap_file ($((needed_kb / 1024)) MB)..."
    fi

    # fallocate first (fast, sparse-safe on ext4/xfs); dd covers the rest.
    if ! as_root fallocate -l "${needed_kb}K" "$swap_file" 2>>"$LOG_FILE"; then
      info "fallocate failed — falling back to dd (slower)."
      as_root dd if=/dev/zero of="$swap_file" bs=1K count="$needed_kb" status=none >>"$LOG_FILE" 2>&1 \
        || { warn "Could not create swap file — skipping swap setup."; return 0; }
    fi

    as_root chmod 600 "$swap_file" >>"$LOG_FILE" 2>&1 || true
    as_root mkswap "$swap_file" >>"$LOG_FILE" 2>&1 \
      || { warn "mkswap failed on $swap_file — skipping swap setup."; return 0; }
  fi

  if [ "$active_kb" -eq 0 ]; then
    as_root swapon "$swap_file" >>"$LOG_FILE" 2>&1 \
      || { warn "swapon failed on $swap_file — skipping swap setup."; return 0; }
  fi

  if ! as_root grep -qE "^${swap_file}[[:space:]]" /etc/fstab 2>/dev/null; then
    info "Persisting $swap_file in /etc/fstab..."
    printf '%s none swap sw 0 0\n' "$swap_file" | as_root tee -a /etc/fstab >/dev/null 2>&1 \
      || warn "Could not persist $swap_file in /etc/fstab — swap will not survive a reboot."
  fi

  local now_kb
  now_kb="$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)"
  info "Swap ready: total swap is now $((now_kb / 1024)) MB."
}

# ---------------------------------------------------------------------------
# Step 3 — Docker + Compose plugin
# ---------------------------------------------------------------------------
install_docker() {
  if have docker && docker compose version >/dev/null 2>&1; then
    info "Docker and Docker Compose already installed ($(docker --version))."
  else
    info "Installing Docker via the official convenience script (get.docker.com)..."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh \
      || fail "Could not download the Docker install script. Check network access to get.docker.com."
    as_root sh /tmp/get-docker.sh >>"$LOG_FILE" 2>&1 \
      || fail "Docker installation failed. See $LOG_FILE for details."
    rm -f /tmp/get-docker.sh
  fi

  if ! docker compose version >/dev/null 2>&1; then
    info "Docker Compose plugin missing, installing separately..."
    if have apt-get; then
      as_root apt-get update -y >>"$LOG_FILE" 2>&1
      as_root apt-get install -y docker-compose-plugin >>"$LOG_FILE" 2>&1
    elif have dnf; then
      as_root dnf install -y docker-compose-plugin >>"$LOG_FILE" 2>&1
    elif have yum; then
      as_root yum install -y docker-compose-plugin >>"$LOG_FILE" 2>&1
    else
      fail "Could not install the Docker Compose plugin automatically: https://docs.docker.com/compose/install/"
    fi
  fi

  if have systemctl; then
    as_root systemctl enable --now docker >>"$LOG_FILE" 2>&1 || true
  fi

  if [ "$(id -u)" -ne 0 ] && have usermod && ! groups "$USER" 2>/dev/null | grep -q '\bdocker\b'; then
    as_root usermod -aG docker "$USER" >>"$LOG_FILE" 2>&1 || true
    warn "Added $USER to the 'docker' group (takes effect on next login). Using sudo for the rest of this run."
  fi
}

docker_compose() {
  if docker ps >/dev/null 2>&1; then
    docker compose "$@"
  else
    as_root docker compose "$@"
  fi
}

# ---------------------------------------------------------------------------
# Step 4 — Clone or update the source
# ---------------------------------------------------------------------------
fetch_source() {
  if [ -d "$JIFFY_INSTALL_DIR/.git" ]; then
    info "Existing install found at $JIFFY_INSTALL_DIR, updating..."
    git -C "$JIFFY_INSTALL_DIR" fetch origin "$JIFFY_REPO_BRANCH" >>"$LOG_FILE" 2>&1
    git -C "$JIFFY_INSTALL_DIR" checkout "$JIFFY_REPO_BRANCH" >>"$LOG_FILE" 2>&1
    git -C "$JIFFY_INSTALL_DIR" pull --ff-only origin "$JIFFY_REPO_BRANCH" >>"$LOG_FILE" 2>&1
  else
    info "Cloning Jiffy Gateway into $JIFFY_INSTALL_DIR..."
    git clone --branch "$JIFFY_REPO_BRANCH" "$JIFFY_REPO_URL" "$JIFFY_INSTALL_DIR" >>"$LOG_FILE" 2>&1 \
      || fail "Could not clone $JIFFY_REPO_URL (branch $JIFFY_REPO_BRANCH)."
  fi
  cd "$JIFFY_INSTALL_DIR"
}

# ---------------------------------------------------------------------------
# Step 5 — Prepare .env
# ---------------------------------------------------------------------------
set_env_value() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i.bak -E "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

current_env_value() { grep -E "^${1}=" .env 2>/dev/null | head -n1 | cut -d'=' -f2-; }

prepare_env_file() {
  if [ ! -f .env ]; then
    info "No .env found, creating one from .env.example..."
    cp .env.example .env
  else
    info ".env already exists — keeping existing values, filling in gaps only."
  fi
  chmod 600 .env

  local secret_key
  secret_key="$(current_env_value SECRET_KEY)"
  if [ -z "$secret_key" ] || [ "$secret_key" = "change-me-in-production" ]; then
    set_env_value SECRET_KEY "$(random_secret)"
    info "Generated SECRET_KEY."
  fi

  local redis_password
  redis_password="$(current_env_value REDIS_PASSWORD)"
  if [ -z "$redis_password" ]; then
    set_env_value REDIS_PASSWORD "$(random_secret)"
    info "Generated REDIS_PASSWORD."
  fi

  local token_key current example
  for token_key in GITHUB_INGEST_TOKEN GITLAB_INGEST_TOKEN GITEA_INGEST_TOKEN; do
    current="$(current_env_value "$token_key")"
    example="$(grep -E "^${token_key}=" .env.example 2>/dev/null | head -n1 | cut -d'=' -f2-)"
    # Regenerate if blank, or if it still matches the value shipped in
    # .env.example (identical, and therefore unsafe, across every fresh
    # checkout).
    if [ -z "$current" ] || [ "$current" = "$example" ]; then
      set_env_value "$token_key" "$(random_secret)"
      info "Generated $token_key."
    fi
  done

  # Anything else still blank and not on the optional allow-list needs a
  # human decision (e.g. a real external credential) — pause instead of
  # guessing, and never prompt interactively (unreliable under curl | bash).
  local pending=() key val optional ok
  while IFS='=' read -r key val; do
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    if [ -z "$val" ]; then
      optional=false
      for ok in "${OPTIONAL_KEYS[@]}"; do
        [ "$key" = "$ok" ] && optional=true
      done
      [ "$optional" = false ] && pending+=("$key")
    fi
  done < .env

  if [ "${#pending[@]}" -gt 0 ]; then
    echo
    echo "Almost there — a few values in $(pwd)/.env need to be filled in by hand"
    echo "before Jiffy can start (these can't be generated automatically):"
    for key in "${pending[@]}"; do echo "  - $key"; done
    echo
    echo "Edit $(pwd)/.env, fill those in, then re-run this exact command."
    info "Paused: waiting on manual .env values: ${pending[*]}"
    exit 0
  fi
}

# ---------------------------------------------------------------------------
# Step 6 — Build, start services, migrate
# ---------------------------------------------------------------------------
start_services() {
  info "Building and starting services (${COMPOSE_SERVICES[*]}) via $COMPOSE_FILE..."
  docker_compose -f "$COMPOSE_FILE" up -d --build "${COMPOSE_SERVICES[@]}" >>"$LOG_FILE" 2>&1 \
    || fail "docker compose up failed. See $LOG_FILE for details."

  info "Waiting for the web container to become responsive..."
  local tries=0
  until docker_compose -f "$COMPOSE_FILE" exec -T web true >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
      fail "web service did not become ready in time. Check: docker compose -f $COMPOSE_FILE logs web"
    fi
    sleep 2
  done

  info "Running database migrations..."
  docker_compose -f "$COMPOSE_FILE" exec -T web uv run python manage.py migrate >>"$LOG_FILE" 2>&1 \
    || fail "Database migration failed. See $LOG_FILE for details."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  info "=== Jiffy Gateway installer starting ==="
  install_basic_tools
  ensure_swap || warn "Swap check did not complete — continuing with the install."
  install_docker
  fetch_source
  prepare_env_file
  start_services
  info "=== Jiffy Gateway is up ==="

  echo
  echo "Jiffy Gateway is running."
  echo "  Install directory: $JIFFY_INSTALL_DIR"
  echo "  Web port:          $(current_env_value WEB_PORT)"
  echo "  Log file:          $LOG_FILE"
  echo
  echo "Next: connect a repository (add the edge component workflow and its"
  echo "shared token) — see README.md > 'Connect a Repository'."
}

main "$@"
