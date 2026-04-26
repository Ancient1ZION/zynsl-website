#!/usr/bin/env bash
# ZYN Empire — install.sh
# One-command bootstrap. Run on a fresh Ubuntu 22.04+ VM.
#
#   curl -fsSL https://raw.githubusercontent.com/ancient1zion/zynsl-website/main/zyn-ops/install.sh | bash
#
# This script is idempotent: re-running upgrades the existing install.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ancient1zion/zynsl-website.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/zyn-empire}"
BRANCH="${BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

log()  { printf "%b\n" "${GREEN}[install]${RESET} $*"; }
warn() { printf "%b\n" "${YELLOW}[install]${RESET} $*" >&2; }
err()  { printf "%b\n" "${RED}[install]${RESET} $*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "missing dependency: $1"; return 1; }
}

# ---------------------------------------------------------------------------
# Step 1: system packages
# ---------------------------------------------------------------------------

log "Installing system packages…"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq git curl ca-certificates python3 python3-pip python3-venv
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y git curl python3 python3-pip
else
  warn "Unsupported package manager; ensure git/python3/pip are present."
fi

# ---------------------------------------------------------------------------
# Step 2: Node + PM2 (for process supervision)
# ---------------------------------------------------------------------------

if ! command -v node >/dev/null 2>&1; then
  log "Installing Node.js (NodeSource LTS)…"
  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi

if ! command -v pm2 >/dev/null 2>&1; then
  log "Installing PM2…"
  sudo npm install -g pm2
fi

# ---------------------------------------------------------------------------
# Step 3: clone or update the repo
# ---------------------------------------------------------------------------

if [ -d "$INSTALL_DIR/.git" ]; then
  log "Updating existing checkout at $INSTALL_DIR…"
  sudo git -C "$INSTALL_DIR" fetch --prune origin
  sudo git -C "$INSTALL_DIR" checkout "$BRANCH"
  sudo git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  log "Cloning $REPO_URL into $INSTALL_DIR…"
  sudo mkdir -p "$(dirname "$INSTALL_DIR")"
  sudo git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
sudo chown -R "$(id -u)":"$(id -g)" "$INSTALL_DIR"

cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# Step 4: Python venv + deps
# ---------------------------------------------------------------------------

VENV="$INSTALL_DIR/.venv"
if [ ! -d "$VENV" ]; then
  log "Creating Python venv at $VENV…"
  "$PYTHON_BIN" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

log "Installing Python dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r "$INSTALL_DIR/zyn-empire-agents/requirements.txt"
pip install --quiet python-dotenv requests loguru gspread google-auth groq google-generativeai

# ---------------------------------------------------------------------------
# Step 5: .env scaffolding
# ---------------------------------------------------------------------------

ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  log "Creating empty .env at $ENV_FILE — fill in secrets before first run."
  cp "$INSTALL_DIR/zyn-empire-agents/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
else
  log ".env already exists; not overwriting."
fi

# ---------------------------------------------------------------------------
# Step 6: logs dir + audits dir
# ---------------------------------------------------------------------------

mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/zyn-ops/audits"

# ---------------------------------------------------------------------------
# Step 7: PM2 — register agent stack + ops daemons
# ---------------------------------------------------------------------------

log "Registering PM2 services…"
pm2 startOrReload "$INSTALL_DIR/zyn-empire-agents/ecosystem.config.js" --update-env || \
  pm2 start "$INSTALL_DIR/zyn-empire-agents/ecosystem.config.js" --update-env

pm2 startOrReload "$INSTALL_DIR/zyn-ops/ecosystem.ops.config.js" --update-env || \
  pm2 start "$INSTALL_DIR/zyn-ops/ecosystem.ops.config.js" --update-env

pm2 save

# Install systemd unit so PM2 starts on boot (no-op if already installed)
if ! systemctl list-unit-files 2>/dev/null | grep -q '^pm2-'; then
  log "Installing PM2 startup hook (systemd)…"
  STARTUP_CMD="$(pm2 startup systemd -u "$(whoami)" --hp "$HOME" | tail -1)"
  if [[ "$STARTUP_CMD" == sudo* ]]; then
    eval "$STARTUP_CMD"
  fi
fi

# ---------------------------------------------------------------------------
# Step 8: post-install summary
# ---------------------------------------------------------------------------

log ""
log "✅ ZYN Empire installation complete."
log ""
log "Install dir : $INSTALL_DIR"
log "Python venv : $VENV"
log "Logs        : $INSTALL_DIR/logs/"
log ""
log "Next steps:"
log "  1. Edit $ENV_FILE and fill in your secrets:"
log "     GROQ_API_KEY, GEMINI_API_KEY, GOOGLE_SA_JSON_PATH, SHEET_ID,"
log "     GAS_PROXY_URL, GITHUB_TOKEN"
log "  2. Run pre-flight: \"$VENV/bin/python\" $INSTALL_DIR/zyn-empire-agents/test_connection.py"
log "  3. If 5/5 pass: pm2 reload all"
log "  4. Watch first 60s: pm2 logs"
log ""
log "Mission control health endpoint: http://<vm-ip>:9090/healthz"
