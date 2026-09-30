#!/bin/sh
# Warns when .env opts into Tailscale (TAILSCALE_ENABLED=true plus a pinned
# TAILSCALE_IP — docker-compose then binds the NATS client/monitor ports to
# that address, see docker-compose.yml) but Tailscale is not actually up on
# this host. Left unchecked, `docker compose up` instead fails on a raw
# "bind: cannot assign requested address" with no hint why.
#
# Tailscale is optional, so this exits silently whenever the feature is off.
#
# Runs two ways:
#   - as the `tailscale-check` one-shot container (profile `tailscale`,
#     enabled automatically by `make dev`/`make start` whenever Tailscale is
#     enabled in .env), talking to the host's tailscaled over its mounted
#     unix socket (ENV_FILE/TS_SOCKET set by docker-compose.yml);
#   - directly on the host via `make check-tailscale` for a manual check.
set -e

ENV_FILE="${ENV_FILE:-$(dirname "$0")/../.env}"

read_env_var() {
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r'
}

TAILSCALE_ENABLED=$(read_env_var TAILSCALE_ENABLED | tr '[:upper:]' '[:lower:]')
TAILSCALE_IP=$(read_env_var TAILSCALE_IP)

# Opt-in switch, defaulting to off when absent or unparseable.
case "$TAILSCALE_ENABLED" in
  true|1|yes|on) ;;
  *) exit 0 ;;
esac

if [ -z "$TAILSCALE_IP" ]; then
  echo "WARNING: TAILSCALE_ENABLED=true in .env but TAILSCALE_IP is empty — the NATS ports will bind every interface (0.0.0.0). Run 'tailscale ip -4' and set TAILSCALE_IP, or set TAILSCALE_ENABLED=false."
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
