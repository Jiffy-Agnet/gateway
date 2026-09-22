#!/bin/bash
# Smoke test for the Jiffy generic sandbox image.
#
# Verifies that version managers (nvm, uv, gvm), provider CLIs (gh, glab, tea),
# and the OpenCode agent CLI are installed and functional.
#
# Usage:
  #   docker build -t jiffy-sandbox:1.3.0 .
#   docker run --rm jiffy-sandbox:1.3.0 bash /smoke-test.sh
set -euo pipefail

PASS=0
FAIL=0
SKIP=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }

echo "=== Sandbox Image Smoke Tests ==="

# --- User context (check first) ---

echo ""
echo "--- User context ---"
CURRENT_USER=$(whoami)
echo "  Running as: $CURRENT_USER"
if [ "$CURRENT_USER" = "jiffy" ] || [ "$CURRENT_USER" = "root" ]; then
  pass "Running as $CURRENT_USER"
else
  fail "Running as unexpected user $CURRENT_USER"
fi

# --- Shell profile files ---

echo ""
echo "--- Shell profiles ---"
if [ -f /home/jiffy/.bashrc ]; then
  pass ".bashrc exists"
else
  fail ".bashrc not found"
fi
if [ -f /home/jiffy/.profile ]; then
  pass ".profile exists"
else
  fail ".profile not found"
fi

# --- PATH check ---

echo ""
echo "--- PATH ---"
echo "  PATH=$PATH"
if echo "$PATH" | grep -q "/usr/local/bin"; then
  pass "/usr/local/bin is in PATH"
else
  fail "/usr/local/bin not in PATH"
fi

# --- Version managers ---

echo ""
echo "--- nvm ---"
if [ -s /usr/local/nvm/nvm.sh ]; then
  . /usr/local/nvm/nvm.sh
  nvm --version >/dev/null 2>&1 && pass "nvm is installed and invokable" || fail "nvm installed but not invokable"
  nvm install 22 >/dev/null 2>&1 && pass "nvm can install Node.js 22" || fail "nvm install 22 failed"
  node --version 2>/dev/null | grep -q "^v22" && pass "Node.js 22 is available" || fail "Node.js 22 not on PATH after nvm install"
else
  fail "nvm.sh not found at /usr/local/nvm/nvm.sh"
fi

echo ""
echo "--- uv ---"
if command -v uv >/dev/null 2>&1; then
  pass "uv is installed and on PATH"
  uv --version >/dev/null 2>&1 && pass "uv responds to --version" || fail "uv --version failed"
else
  fail "uv not found on PATH"
fi

echo ""
echo "--- gvm ---"
if [ -s /usr/local/gvm/scripts/gvm ]; then
  . /usr/local/gvm/scripts/gvm
  gvm version >/dev/null 2>&1 && pass "gvm is installed and invokable" || fail "gvm installed but not invokable"
else
  fail "gvm scripts not found at /usr/local/gvm/scripts/gvm"
fi

# --- Provider CLIs ---

echo ""
echo "--- gh (GitHub CLI) ---"
if command -v gh >/dev/null 2>&1; then
  gh --version >/dev/null 2>&1 && pass "gh is installed and responds to --version" || fail "gh --version failed"
else
  fail "gh not found on PATH"
fi

echo ""
echo "--- glab (GitLab CLI) ---"
if command -v glab >/dev/null 2>&1; then
  glab --version >/dev/null 2>&1 && pass "glab is installed and responds to --version" || fail "glab --version failed"
else
  fail "glab not found on PATH"
fi

echo ""
echo "--- tea (Gitea CLI) ---"
if command -v tea >/dev/null 2>&1; then
  tea version >/dev/null 2>&1 && pass "tea is installed and responds to version" || fail "tea version failed"
else
  skip "tea not installed (optional)"
fi

echo ""
echo "--- iptables ---"
if command -v iptables >/dev/null 2>&1; then
  pass "iptables is installed (network egress restriction)"
else
  fail "iptables not found"
fi

# --- OpenCode agent CLI ---

echo ""
echo "--- opencode ---"
if command -v opencode >/dev/null 2>&1; then
  opencode version >/dev/null 2>&1 && pass "opencode is installed and responds to version" || fail "opencode version failed"
  # Verify it's accessible from /usr/local/bin (not just root's ~/.local/bin)
  if [ -x /usr/local/bin/opencode ]; then
    pass "opencode copied to /usr/local/bin and executable"
  else
    fail "opencode not found at /usr/local/bin"
  fi
else
  fail "opencode not found on PATH"
fi

# --- Summary ---

echo ""
echo "=== Results: $PASS passed, $SKIP skipped, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
