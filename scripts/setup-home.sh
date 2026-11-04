#!/usr/bin/env bash
# One-time setup of a macOS / Linux computer (or Raspberry Pi) as the "home" checker.
#   curl -fsSL https://raw.githubusercontent.com/DimaDerepa/bfbot2/main/scripts/setup-home.sh | bash
set -euo pipefail
REPO="DimaDerepa/bfbot2"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS-$ARCH" in
  Darwin-arm64) PKG=osx-arm64 ;; Darwin-x86_64) PKG=osx-x64 ;;
  Linux-x86_64) PKG=linux-x64 ;; Linux-aarch64|Linux-arm64) PKG=linux-arm64 ;; Linux-armv7l) PKG=linux-arm ;;
  *) echo "Unsupported system $OS-$ARCH"; exit 1 ;;
esac
command -v git >/dev/null || { if [ "$OS" = Darwin ]; then xcode-select --install || true; else sudo apt-get update && sudo apt-get install -y git curl; fi; }
mkdir -p "$HOME/actions-runner" && cd "$HOME/actions-runner"
if [ ! -f ./config.sh ]; then
  URL=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | grep -o "https://[^\"]*actions-runner-$PKG-[0-9.]*\.tar\.gz" | head -1)
  curl -fsSL "$URL" -o runner.tgz && tar xzf runner.tgz && rm runner.tgz
fi
echo
echo "Open https://github.com/$REPO/settings/actions/runners/new and copy the value after --token"
read -r -p "Paste the token here: " TOKEN < /dev/tty
./config.sh --url "https://github.com/$REPO" --token "$TOKEN" --unattended --replace --name "home-$(hostname -s)"
if [ "$OS" = Darwin ]; then ./svc.sh install && ./svc.sh start; else sudo ./svc.sh install && sudo ./svc.sh start; fi
echo
echo "Last step: create the variable HOME_RUNNER = true at https://github.com/$REPO/settings/variables/actions/new"
