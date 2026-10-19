#!/usr/bin/env bash
# One-shot setup for macOS — no Homebrew required.
# Paste in Terminal:
#   curl -fsSL https://raw.githubusercontent.com/torksabig/aether-agents/main/scripts/mac-setup.sh | bash

set -euo pipefail

REPO_URL="${AETHER_REPO_URL:-https://github.com/torksabig/aether-agents.git}"
TARGET_DIR="${AETHER_DIR:-$HOME/aether-agent-platform}"

# Remove earlier broken Homebrew path lines (if any)
if [[ -f "$HOME/.zprofile" ]]; then
  grep -v '/opt/homebrew/bin/brew' "$HOME/.zprofile" >"$HOME/.zprofile.aether.tmp" || true
  mv "$HOME/.zprofile.aether.tmp" "$HOME/.zprofile"
fi
# Prefer the AWS bootstrap if you need brew+docker:
#   curl -fsSL https://raw.githubusercontent.com/torksabig/aether-agents/main/scripts/mac-aws-bootstrap.sh | bash

echo "==> Checking Python 3…"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found."
  echo "Install from https://www.python.org/downloads/ OR run: xcode-select --install"
  exit 1
fi
python3 --version

if ! command -v git >/dev/null 2>&1; then
  echo "git not found. Run: xcode-select --install"
  exit 1
fi

if [[ ! -d "$TARGET_DIR/.git" ]]; then
  echo "==> Cloning into ${TARGET_DIR}…"
  git clone "$REPO_URL" "$TARGET_DIR"
else
  echo "==> Repo already at ${TARGET_DIR} — pulling latest…"
  git -C "$TARGET_DIR" pull --ff-only || true
fi

cd "$TARGET_DIR"

echo "==> Creating virtualenv…"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing aether…"
python3 -m pip install -U pip
python3 -m pip install -e ".[dev]"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Created .env — add NOTION_TOKEN later for Notion writes."
fi

echo
echo "Done."
echo "  cd ${TARGET_DIR}"
echo "  source .venv/bin/activate"
echo "  aether"
echo
echo "Quick test:"
echo "  aether ask Demo Building Owner Oy demo-building.fi"
