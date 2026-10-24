#!/usr/bin/env bash
# One-command setup for Linux, Raspberry Pi OS and macOS: installs build dependencies, builds the hub, runs tests.
#   ./setup.sh          laptop / desktop (hub + simulator)
#   ./setup.sh --pi     also installs the camera/MediaPipe packages for the Raspberry Pi agent
# Safe to rerun. Set SKIP_DEPS=1 to skip the package-manager step. Nothing here needs an account or costs money.
set -euo pipefail
cd "$(dirname "$0")"
PI=0; [[ "${1:-}" == "--pi" ]] && PI=1
say() { printf '\n== %s\n' "$*"; }

say "Checking Python"
command -v python3 >/dev/null || { echo "python3 (3.8+) is required"; exit 1; }
python3 --version

if [[ "${SKIP_DEPS:-0}" != "1" ]]; then
  say "Installing build dependencies"
  if command -v apt-get >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y build-essential cmake ninja-build libboost-dev libssl-dev libsqlite3-dev nlohmann-json3-dev python3-venv
  elif command -v dnf >/dev/null; then
    sudo dnf install -y gcc-c++ cmake ninja-build boost-devel openssl-devel sqlite-devel json-devel
  elif command -v brew >/dev/null; then
    brew install cmake ninja boost openssl sqlite nlohmann-json
    export OPENSSL_ROOT_DIR="$(brew --prefix openssl)"
  else
    echo "Unknown package manager. Install manually: a C++20 compiler, CMake, Ninja, Boost headers, OpenSSL, SQLite3, nlohmann-json."
    exit 1
  fi
fi

say "Creating .env (secrets file) if missing"
if [[ ! -f .env ]]; then cp .env.example .env; echo "created .env (add ANTHROPIC_API_KEY only if you want the AI app)"; else echo ".env already exists, left alone"; fi

say "Building the hub"
python3 ips.py build

say "Running unit tests"
python3 ips.py test

if [[ $PI == 1 ]]; then
  say "Installing Raspberry Pi agent packages"
  if command -v apt-get >/dev/null; then sudo apt-get install -y python3-picamera2 || echo "picamera2 not available via apt (only needed for CSI cameras)"; fi
  python3 -m venv .venv --system-site-packages
  .venv/bin/pip install -r requirements-pi.txt
  echo "Run the agent with:  .venv/bin/python edge/agent.py --hub <laptop-ip>:8080 --source picamera --cams 0 1 --os-pointer"
fi

printf '\nSetup complete. Try it:\n  python3 ips.py start\n  python3 ips.py open\n  python3 ips.py sim\n  python3 ips.py stop\n'
