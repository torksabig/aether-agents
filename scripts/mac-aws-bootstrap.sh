#!/usr/bin/env bash
# Fix Mac Terminal for Aether AWS deploy: Homebrew + git + awscli + Docker + clone.
# Paste ONE line in Terminal (no sudo password in this paste — installer will ask):
#
#   curl -fsSL https://raw.githubusercontent.com/torksabig/aether-agents/main/scripts/mac-aws-bootstrap.sh | bash
#
set -euo pipefail

TARGET_DIR="${AETHER_DIR:-$HOME/aether-agent-platform}"
REPO_URL="${AETHER_REPO_URL:-https://github.com/torksabig/aether-agents.git}"

echo "==> Cleaning broken Homebrew lines in ~/.zprofile (if any)"
if [[ -f "$HOME/.zprofile" ]]; then
  # Remove lines that call missing /opt/homebrew/bin/brew
  grep -v '/opt/homebrew/bin/brew' "$HOME/.zprofile" >"$HOME/.zprofile.aether.tmp" || true
  mv "$HOME/.zprofile.aether.tmp" "$HOME/.zprofile"
fi

# --- Homebrew ---
if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
  eval "$(/usr/local/bin/brew shellenv)"
else
  echo "==> Installing Homebrew (enter your Mac password when asked)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null \
      || echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >>"$HOME/.zprofile"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
    grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null \
      || echo 'eval "$(/usr/local/bin/brew shellenv)"' >>"$HOME/.zprofile"
  else
    echo "Homebrew install finished but brew not on PATH. Open a new Terminal tab and re-run." >&2
    exit 1
  fi
fi

echo "==> brew $(brew --version | head -1)"

echo "==> Installing git, python, awscli…"
brew install git python awscli

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker Desktop (open it once after install)…"
  brew install --cask docker
  echo "    Open Docker.app from Applications, wait until it says Running, then continue."
  open -a Docker 2>/dev/null || true
fi

# Xcode CLT for git/python fallbacks
if ! xcode-select -p >/dev/null 2>&1; then
  echo "==> Installing Xcode Command Line Tools…"
  xcode-select --install || true
fi

echo "==> Cloning / updating repo at ${TARGET_DIR}"
if [[ ! -d "$TARGET_DIR/.git" ]]; then
  git clone "$REPO_URL" "$TARGET_DIR"
else
  git -C "$TARGET_DIR" pull --ff-only || true
fi

cd "$TARGET_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Created .env — open it and set NOTION_TOKEN (and keep data source IDs)."
fi

echo
echo "=========================================="
echo "Done. Next commands (copy exactly):"
echo
echo "  cd ~/aether-agent-platform"
echo "  open -e .env          # paste NOTION_TOKEN, save"
echo "  aws configure         # Access key, secret, region eu-north-1"
echo "  # wait until Docker Desktop whale icon is idle"
echo "  ./scripts/aws-deploy.sh"
echo "  ./scripts/aws-worker-schedule.sh"
echo "=========================================="
