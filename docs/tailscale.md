# Securing NATS with Tailscale

> **Optional — not a prerequisite.** Everything below is gated behind
> `TAILSCALE_ENABLED` in `.env`, which defaults to `false`. With it off the
> stack runs exactly as before Tailscale existed. Turn it on when you want the
> transport hardening described here; leave it off for local dev, or if you
> secure NATS some other way (firewall rules, a private network, your own VPN).

With the switch off, `docker-compose.yml` publishes the NATS client, NATS
monitoring and Postgres ports on every interface of the host (`0.0.0.0`) —
on a VPS, the public internet — and `NATS_TOKEN` / the Postgres password are
the only protection. With it on, the stack gets its own node on your
[Tailscale](https://tailscale.com/) tailnet (a WireGuard mesh VPN) and the
host ports close to the outside world:

```
                 tailnet (WireGuard, policy-filtered)
engine / worker ─────────────► algo-broker:4222 ─┐  `tailscale` container
operator laptop ─────────────► algo-broker:8222 ─┤  (Tailscale Serve,
                               algo-broker:5432 ─┤   serve.json, rendered
                                                 │    from .env)
                                                 │
                                   trading_net   ├─► nats:4222 / nats:8222
                                                 └─► postgres:5432

public internet ──► https://<FQDN>/ ──► broker:80
                    (Serve HTTPS + Funnel — the only route to the broker's
                     HTTP API; port 80 itself is on neither the host's
                     public address nor the tailnet)
host ports: 127.0.0.1:{NATS_PORT,NATS_MONITOR_PORT,POSTGRES_PORT,WEBHOOK_PORT}
            — nothing on the VPS' public address at all
```

| | `TAILSCALE_ENABLED=false` (default) | `TAILSCALE_ENABLED=true` |
| --- | --- | --- |
| `tailscale` container | not pulled, not started | started (profile `tailscale`) |
| NATS 4222 / 8222 on the host | `0.0.0.0` | `127.0.0.1` |
| Postgres on the host | `0.0.0.0` | `127.0.0.1` |
| `WEBHOOK_PORT` on the host | `0.0.0.0` | `127.0.0.1` |
| TradingView posts to | `http://<host address>:WEBHOOK_PORT/…` | `https://<FQDN>/<prefix>/secret/webhook` (Funnel) |
| Remote NATS clients connect to | `nats://<host address>:NATS_PORT` | `nats://<TAILSCALE_HOSTNAME>:4222` |
| Who may connect | anyone holding `NATS_TOKEN` | tailnet members the policy allows, *and* holding `NATS_TOKEN` |

`make dev` / `make start` / `make stop` read the switch and derive the rest
(the compose profile and the bind address, `PRIVATE_BIND_ADDRESS`) — there is
no IP to copy into `.env`, and nothing to install on the host. A bare
`docker compose up` does not read the switch: it binds `0.0.0.0` and does not
start the container, so use the `make` targets on a server.

The container runs Tailscale in userspace mode — no `/dev/net/tun`, no
`NET_ADMIN` — and forwards connections with Tailscale Serve instead of sharing
its network namespace with NATS, so restarting it only makes remote NATS
clients reconnect; the broker's own NATS connection is never touched.

## Setup

1. **Tailnet policy.** Paste [`config/tailscale/policy.hujson`](../config/tailscale/policy.hujson)
   into the admin console's [Access controls](https://login.tailscale.com/admin/acls/file)
   (or push it with `tailscale/gitops-acl-action`). It defines one tag per
   role — `tag:broker`, `tag:engine`, `tag:ingester`, `tag:worker` — and allows
   only the NATS connections the pipeline makes:

   | From | To | Port |
   | --- | --- | --- |
   | `tag:ingester` | `tag:engine` | 4222 |
   | `tag:engine`, `tag:worker` | `tag:broker` | 4222 |
   | admins' own devices | every tagged node | any (monitoring 8222, Postgres 5432, SSH) |

   Everything else is denied, and the file's `tests` block makes the console
   refuse an edit that would break one of these paths. Enable **MagicDNS**
   under [DNS](https://login.tailscale.com/admin/dns) so nodes resolve each
   other by name.

2. **Auth key.** [Settings → Keys](https://login.tailscale.com/admin/settings/keys)
   → *Generate auth key*: tag it `tag:broker`, pre-approved. A tagged node's
   key never expires, so the broker does not fall off the tailnet after 180
   days.

3. **`.env`:**

   ```env
   TAILSCALE_ENABLED=true
   TAILSCALE_AUTHKEY=tskey-auth-...
   TAILSCALE_HOSTNAME=algo-broker      # the MagicDNS name other servers use
   TAILSCALE_DNS_NAME=rudd-komodo.ts.net   # the tailnet's domain (admin console → DNS)
   ```

   The two names together are this node's FQDN, which Tailscale Serve needs to
   bind its HTTPS handler. `make start`/`make dev` render it into
   `config/tailscale/serve.json` from `serve.json.template`; the rendered file
   is generated, not tracked, because `tailscaled` reads no environment
   variables inside it. Run `make tailscale-serve-config` by itself after
   editing either name — the directory is watched, so a running container
   picks it up without a restart.

   What the rendered file serves:

   | Serve entry | Reachable from | Forwards to |
   | --- | --- | --- |
   | TCP 4222 / 8222 | tailnet (policy-filtered) | `nats:4222` / `nats:8222` |
   | TCP 5432 | tailnet (admin devices only) | `postgres:5432` |
   | HTTPS 443 `/` | **the public internet** (`AllowFunnel`) | `http://broker:WEBHOOK_PORT` |

   The broker's HTTP port is **not** forwarded onto the tailnet, and with
   Tailscale on it is not published on the host's public address either (see
   the table above). Funnel on 443 is therefore the only route to the broker's
   HTTP API, and `@WEBHOOK_PORT@` in the template is rendered from the same
   `.env` the app reads, so the proxy target follows `WEBHOOK_PORT` when it
   moves.

   > **Do not narrow this handler to the webhook path.** It is the obvious
   > hardening idea and it breaks every signal. Tailscale Serve strips the
   > mount point before proxying, and a mount matches its whole subtree —
   > measured against a live tailnet:
   >
   > | Mount | Request | What the broker receives |
   > | --- | --- | --- |
   > | `/<prefix>/secret/webhook` | the same path | `POST /` → **404** |
   > | `/probeA` | `/probeA/hello` | `GET /hello` |
   > | `/probeA/` | `/probeA/hello` | `GET /hello` |
   > | `/` | `/<prefix>/v1/health` | unchanged → `200` |
   >
   > So mounting the webhook's own path makes the broker answer 404 for every
   > alert, and the shallowest mount that would leave `/secret/webhook` intact
   > is `/<prefix>`, which forwards `/v1` and `/admin` too. Exposing a single
   > path needs a route that the stripped remainder can hit — an application
   > change, not a `serve.json` one.
   > `test_the_funnel_handler_stays_mounted_at_the_root` pins this.

   The 443 handler is a [Funnel](https://tailscale.com/kb/1223/funnel): it
   gives the node a public, Tailscale-terminated HTTPS name, so TradingView
   (which is not on the tailnet) can POST to
   `https://<TAILSCALE_HOSTNAME>.<TAILSCALE_DNS_NAME>/secret/webhook` without
   the host publishing `WEBHOOK_PORT` itself. It is the one entry in the file
   that is **not** tailnet-only — Funnel bypasses the tailnet policy
   entirely, so everything FastAPI serves is reachable from the internet
   behind `BROKER_API_PREFIX`, and the webhook token plus the
   `X-API-KEY` guard are what stand in front of it. The `*.ts.net` name is
   discoverable from public Certificate Transparency logs, so treat the
   prefix as the secret it is and rotate it with `WEBHOOK_SECRET`. Funnel
   also has to
   be enabled for the tailnet in the admin console
   ([Settings → Funnel](https://login.tailscale.com/admin/settings/funnel)) and
   the node needs `funnel` in its policy attributes, or the handler is
   rejected at startup. Drop the `Web` and `AllowFunnel` blocks from
   `serve.json.template` if you do not want a public endpoint — keep sending
   TradingView to the published `WEBHOOK_PORT` instead.

4. **Start** — `make start` (or `make dev`), then check it joined:

   ```bash
   make tailscale-status   # tailnet peers + the Serve forwards and HTTPS handler
   ```

   The key is only used for the first login: the node's identity lives in the
   `tailscale_state` volume and `TS_AUTH_ONCE` keeps a restart from logging in
   again, so a single-use key is fine. Left empty, the container prints a
   login URL instead (`make tailscale-status` shows it).

**On every other server** (engine, the MT5 VPS running the ingester and the
worker): install Tailscale — or run the same image beside a Docker stack —
join with a key tagged for that role, and point its NATS client at
`nats://<TAILSCALE_HOSTNAME>:4222` with the matching `NATS_TOKEN`. A client
inside Docker without host networking cannot resolve MagicDNS; give it the
broker node's tailnet IP (`make tailscale-status`) instead.

**To turn it off**, set `TAILSCALE_ENABLED=false`, then `make stop` and
`make start`: `make stop` always takes the `tailscale` container down with the
rest, and the ports go back to `0.0.0.0`. `NATS_TOKEN` is then your only NATS
protection, so keep the ports behind a firewall if the host is not local. The
node stays in the tailnet (offline) until you remove it from the admin
console; the `tailscale_state` volume lets it rejoin as the same node later.
