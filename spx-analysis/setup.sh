#!/usr/bin/env bash
# Prepares a cloud session to read SPX from TradingView:
#   1. installs the tradingview-mcp server (CLI + MCP) in ~/tradingview-mcp
#   2. starts headless Chromium with CDP on :9222 showing the SPX chart
#   3. dismisses TradingView popups that cover the chart
set -euo pipefail

NODE="$(command -v node || true)"
[ -z "$NODE" ] && [ -x /opt/node22/bin/node ] && NODE=/opt/node22/bin/node
[ -z "$NODE" ] && { echo "ERROR: node not found" >&2; exit 1; }
NPM="$(dirname "$NODE")/npm"
TV_DIR="$HOME/tradingview-mcp"
TV="$NODE $TV_DIR/src/cli/index.js"
CHROME="${CHROME:-/opt/pw-browsers/chromium}"
PROFILE="$HOME/.spx-tv-profile"
SYMBOL="${SYMBOL:-SP:SPX}"

if [ ! -d "$TV_DIR/.git" ]; then
  git clone --depth 1 https://github.com/tradesdontlie/tradingview-mcp.git "$TV_DIR"
fi
if [ ! -d "$TV_DIR/node_modules" ]; then
  (cd "$TV_DIR" && "$NPM" install --no-audit --no-fund >/dev/null)
fi

if ! curl -s -m 2 http://localhost:9222/json/version >/dev/null; then
  mkdir -p "$PROFILE"
  PROXY_ARG=()
  [ -n "${HTTPS_PROXY:-}" ] && PROXY_ARG=(--proxy-server="$HTTPS_PROXY")
  nohup "$CHROME" --headless=new --no-sandbox --disable-dev-shm-usage \
    --remote-debugging-port=9222 --user-data-dir="$PROFILE" "${PROXY_ARG[@]}" \
    --window-size=1600,900 "https://www.tradingview.com/chart/?symbol=${SYMBOL}" \
    >"$HOME/.spx-chromium.log" 2>&1 &
  for _ in $(seq 1 30); do curl -s -m 2 http://localhost:9222/json/version >/dev/null && break; sleep 1; done
fi

# wait until the chart API answers
for _ in $(seq 1 30); do
  if $TV status 2>/dev/null | grep -q '"api_available": true'; then break; fi
  sleep 2
done
$TV status | grep -q '"cdp_connected": true' || { echo "ERROR: CDP not connected" >&2; exit 1; }

$TV symbol "$SYMBOL" >/dev/null
sleep 4
$TV ui keyboard Escape >/dev/null 2>&1 || true
$TV ui eval "(()=>{let n=0;document.querySelectorAll('button').forEach(b=>{const t=(b.innerText||'').trim();const a=b.getAttribute('aria-label')||'';if(/^Got it/.test(t)||/close/i.test(a)){b.click();n++}});return n})()" >/dev/null 2>&1 || true
echo "READY: $($TV status | grep -E 'chart_symbol|cdp_connected' | tr -d ' \n')"
