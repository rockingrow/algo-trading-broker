"""
Consistency checks for the optional Tailscale node.

Three files describe one setup and none of them is validated by anything this
repository runs: ``docker-compose.yml`` (the ``tailscale`` service and the
ports it forwards to), ``config/tailscale/serve.json.template`` (what the node
exposes, rendered to ``serve.json`` from ``.env`` by ``make
tailscale-serve-config``, so the template is what is tracked and checked here) and ``config/tailscale/policy.hujson`` (who may reach it,
applied by hand in the Tailscale admin console). A drift between them fails
only on a server, as a NATS client that cannot connect — so it is pinned here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
TAILSCALE_DIR = REPO_ROOT / "config" / "tailscale"
# Any syntactically valid tailnet name: the tests check structure, not the name
# of one deployment.
FQDN = "algo-broker.example-tailnet.ts.net"
# The port the broker listens on, which make reads from the same .env. The
# template must not hardcode it: Funnel's handler is the only route to the
# broker, so a stale port here is a dead webhook.
WEBHOOK_PORT = "80"


def _strip_hujson(text: str) -> str:
  """Drop ``//`` comments (outside strings) and trailing commas, turning the
  HuJSON policy file into plain JSON."""
  output: list[str] = []
  in_string = False
  index = 0
  while index < len(text):
    char = text[index]
    if in_string:
      output.append(char)
      if char == "\\":
        output.append(text[index + 1])
        index += 1
      elif char == '"':
        in_string = False
    elif char == '"':
      in_string = True
      output.append(char)
    elif text.startswith("//", index):
      while index < len(text) and text[index] != "\n":
        index += 1
      continue
    else:
      output.append(char)
    index += 1
  return re.sub(r",(\s*[}\]])", r"\1", "".join(output))


@pytest.fixture(scope="module")
def policy() -> dict:
  return json.loads(_strip_hujson((TAILSCALE_DIR / "policy.hujson").read_text()))


@pytest.fixture(scope="module")
def serve_config() -> dict:
  """The template with a stand-in FQDN, as ``make tailscale-serve-config``
  renders it. The rendered ``serve.json`` is generated and untracked, so it
  cannot be what this suite reads."""
  template = (TAILSCALE_DIR / "serve.json.template").read_text()
  rendered = template.replace("@TAILSCALE_FQDN@", FQDN)
  return json.loads(rendered.replace("@WEBHOOK_PORT@", WEBHOOK_PORT))


@pytest.fixture(scope="module")
def compose() -> dict:
  return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())


def _tag_of(target: str) -> str | None:
  """``tag:broker:4222`` → ``tag:broker``; anything that is not a tag → None."""
  if not target.startswith("tag:"):
    return None
  return ":".join(target.split(":")[:2])


def test_every_tag_the_policy_uses_is_owned(policy):
  owned = set(policy["tagOwners"])
  used: set[str] = set()
  for grant in policy["grants"]:
    used.update(entry for entry in grant["src"] + grant["dst"])
  for case in policy["tests"]:
    used.add(case["src"])
    used.update(case.get("accept", []) + case.get("deny", []))
  tags = {tag for tag in (_tag_of(entry) for entry in used) if tag}

  assert tags <= owned, f"tags used but not in tagOwners: {sorted(tags - owned)}"


def test_broker_node_advertises_an_owned_tag(policy, compose):
  extra_args = compose["services"]["tailscale"]["environment"]["TS_EXTRA_ARGS"]
  advertised = re.search(r"--advertise-tags=(\S+)", extra_args).group(1).split(",")

  assert set(advertised) <= set(policy["tagOwners"])


def test_every_port_granted_to_the_broker_is_served(policy, serve_config):
  served_ports = set(serve_config["TCP"])
  for grant in policy["grants"]:
    if "tag:broker" not in grant["dst"]:
      continue
    for rule in grant["ip"]:
      if rule == "*":
        continue
      port = rule.split(":")[-1]
      assert port in served_ports, f"{rule} is granted but not forwarded"


def test_serve_forwards_reach_the_ports_the_services_listen_on(serve_config, compose):
  services = compose["services"]
  nats_command = services["nats"]["command"]
  listening = {
    "nats": {
      nats_command[nats_command.index("--port") + 1],
      nats_command[nats_command.index("--http_port") + 1],
    },
    "postgres": {"5432"},
    "broker": {services["broker"]["environment"]["WEBHOOK_PORT"].strip('"$ {}').split(":-")[-1]},
  }
  for tailnet_port, handler in serve_config["TCP"].items():
    # The HTTPS entry has no TCPForward: its backend is in ``Web`` below.
    if "TCPForward" not in handler:
      continue
    host, port = handler["TCPForward"].rsplit(":", 1)
    assert host in listening, f"tailnet port {tailnet_port} forwards to {host}"
    assert port in listening[host], f"{host} does not listen on {port}"
    assert "trading_net" in services["tailscale"]["networks"]
    assert "trading_net" in services[host]["networks"]


def test_tailscale_service_is_opt_in_and_reads_the_mounted_serve_config(compose):
  service = compose["services"]["tailscale"]
  serve_path = service["environment"]["TS_SERVE_CONFIG"]
  mounts = dict(volume.split(":")[:2] for volume in service["volumes"])

  assert service["profiles"] == ["tailscale"]
  assert mounts["./config/tailscale"] == str(Path(serve_path).parent)
  # Without TS_AUTH_ONCE a restart would log in again with a spent key.
  assert service["environment"]["TS_AUTH_ONCE"] == "true"


@pytest.mark.parametrize("service_name", ["nats", "postgres", "broker"])
def test_private_ports_follow_the_makefile_bind_address(compose, service_name):
  """The broker is in this list for the same reason as the other two: with
  Tailscale on, the only public route in is the Funnel webhook path, and a
  host port published on 0.0.0.0 would quietly reopen all of FastAPI to the
  VPS' public address."""
  for mapping in compose["services"][service_name]["ports"]:
    assert mapping.startswith("${PRIVATE_BIND_ADDRESS:-0.0.0.0}:"), mapping


def test_the_funnel_handler_stays_mounted_at_the_root(serve_config):
  """Narrowing this handler to the webhook path looks like an obvious
  hardening win and silently breaks every signal.

  Tailscale Serve strips the mount point before proxying, and a mount matches
  its whole subtree. Measured against a live tailnet: mounting
  ``/<prefix>/secret/webhook`` made the broker receive ``POST /`` and answer
  404, while mounting ``/probeA`` turned ``/probeA/hello`` into ``/hello``.
  The shallowest mount that would leave ``/secret/webhook`` intact is
  ``/<prefix>``, which forwards ``/v1`` and ``/admin`` as well. Restricting
  Funnel to one path therefore needs a route the stripped remainder can hit,
  which is an application change — so this test fails loudly rather than let
  the config change alone."""
  funnelled = set(serve_config["AllowFunnel"])

  assert funnelled == {f"{FQDN}:443"}
  for host_port in funnelled:
    handlers = serve_config["Web"][host_port]["Handlers"]
    assert set(handlers) == {"/"}, (
      "Tailscale strips the mount point: a deeper mount makes the broker "
      "receive a path that matches no route"
    )


def test_the_broker_http_port_is_not_forwarded_onto_the_tailnet(serve_config, policy):
  """Port 80 is closed on the tailnet side as well: no TCP forward, and no
  grant that would promise one."""
  for handler in serve_config["TCP"].values():
    assert handler.get("TCPForward") != "broker:80"

  for grant in policy["grants"]:
    if "tag:broker" not in grant["dst"]:
      continue
    assert "tcp:80" not in grant["ip"]


def test_the_funnel_handler_proxies_the_port_the_broker_listens_on(serve_config, compose):
  """Funnel is the only route to the broker, so the handler's port has to
  track WEBHOOK_PORT. Hardcoding 80 here breaks the webhook with a 502 the
  moment the port moves — and nothing else would catch it."""
  broker_port = (
    compose["services"]["broker"]["environment"]["WEBHOOK_PORT"]
    .strip('"$ {}')
    .split(":-")[-1]
  )
  for entry in serve_config["Web"].values():
    for handler in entry["Handlers"].values():
      assert handler["Proxy"] == f"http://broker:{broker_port}"
