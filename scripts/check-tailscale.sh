#!/bin/sh
# Warns when .env pins TAILSCALE_IP (docker-compose then binds the NATS
# client/monitor ports to it — see docker-compose.yml) but Tailscale is not
# actually up on this host. Left unchecked, `docker compose up` instead fails
# on a raw "bind: cannot assign requested address" with no hint why.
#
# Runs two ways:
#   - as the `tailscale-check` one-shot container (profile `tailscale`,
#     enabled automatically by `make dev`/`make start` whenever TAILSCALE_IP
#     is set), talking to the host's tailscaled over its mounted unix socket
#     (ENV_FILE/TS_SOCKET set by docker-compose.yml);
#   - directly on the host via `make check-tailscale` for a manual check.
set -e

ENV_FILE="${ENV_FILE:-$(dirname "$0")/../.env}"

TAILSCALE_IP=$(grep -E '^TAILSCALE_IP=' "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-)
TAILSCALE_IP=$(printf '%s' "$TAILSCALE_IP" | tr -d '\r')

if [ -z "$TAILSCALE_IP" ]; then
  exit 0
fi

# On Windows the CLI ships at this fixed path but the installer does not
# add it to Git Bash's PATH (only used by the host-side `make check-tailscale`
# run; the container's tailscale-check has its own PATH from its own image).
if ! command -v tailscale >/dev/null 2>&1 && [ -d "/c/Program Files/Tailscale" ]; then
  PATH="$PATH:/c/Program Files/Tailscale"
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "WARNING: TAILSCALE_IP=$TAILSCALE_IP is set in .env but the 'tailscale' CLI is not installed on this host — the NATS ports will fail to bind."
  exit 0
fi

if [ -n "$TS_SOCKET" ]; then
  CURRENT_IP=$(tailscale --socket="$TS_SOCKET" ip -4 2>/dev/null || true)
else
  CURRENT_IP=$(tailscale ip -4 2>/dev/null || true)
fi

if [ -z "$CURRENT_IP" ]; then
  echo "WARNING: Tailscale does not appear to be running on this host (no tailnet IPv4 address). TAILSCALE_IP=$TAILSCALE_IP is set in .env, so 'docker compose up' will fail to bind the NATS ports until you run 'tailscale up'."
  exit 0
fi

if [ "$CURRENT_IP" != "$TAILSCALE_IP" ]; then
  echo "WARNING: this host's current Tailscale IPv4 address ($CURRENT_IP) does not match TAILSCALE_IP ($TAILSCALE_IP) in .env — update .env or re-check 'tailscale ip -4'."
  exit 0
fi

echo "Tailscale OK — bound to $CURRENT_IP"
