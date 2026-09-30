"""
Consistency checks for the optional Tailscale node.

Three files describe one setup and none of them is validated by anything this
repository runs: ``docker-compose.yml`` (the ``tailscale`` service and the
ports it forwards to), ``config/tailscale/serve.json`` (what the node exposes
on the tailnet) and ``config/tailscale/policy.hujson`` (who may reach it,
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
  return json.loads((TAILSCALE_DIR / "serve.json").read_text())


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
  }
  for tailnet_port, handler in serve_config["TCP"].items():
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


@pytest.mark.parametrize("service_name", ["nats", "postgres"])
def test_private_ports_follow_the_makefile_bind_address(compose, service_name):
  for mapping in compose["services"][service_name]["ports"]:
    assert mapping.startswith("${PRIVATE_BIND_ADDRESS:-0.0.0.0}:"), mapping
