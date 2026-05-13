#!/usr/bin/env python
import re
from pathlib import Path

import zmq
from dotenv import dotenv_values


def is_valid_z85(k: str) -> bool:
  return bool(k) and len(k) == 40 and "<" not in k


def _write_key(env_path: Path, key: str, value: str) -> None:
  """Replace all occurrences of `key` in the file, or append if absent.

  Uses a direct write instead of os.replace() so it works on Docker bind mounts.
  Values are wrapped in SINGLE quotes — the Z85 alphabet contains '#' and '$',
  which double-quoted dotenv/bash parsers would treat as comments or variable
  expansion respectively. Single quotes are literal in every parser we touch
  (python-dotenv, godotenv via docker-compose env_file, bash `source`), and
  Z85 itself contains no single quote, so the value never needs escaping.
  """
  content = env_path.read_text()
  pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
  replacement = f"{key}='{value}'"
  if pattern.search(content):
    content = pattern.sub(replacement, content)
  else:
    content = content.rstrip("\n") + f"\n{replacement}\n"
  env_path.write_text(content)


def ensure_keys():
  env_file = Path(".env")

  if not env_file.exists():
    print(f"Creating missing {env_file}")
    env_file.touch()

  # Parse effective values (last occurrence wins, same as pydantic-settings)
  vals = dotenv_values(env_file)
  pub = (vals.get("ZMQ_CURVE_SERVER_PUBLIC_KEY") or "").strip().strip('"').strip("'")
  sec = (vals.get("ZMQ_CURVE_SERVER_SECRET_KEY") or "").strip().strip('"').strip("'")

  if is_valid_z85(pub) and is_valid_z85(sec):
    # Keys are valid — deduplicate any repeated entries left by previous runs
    _write_key(env_file, "ZMQ_CURVE_ENABLED", "true")
    _write_key(env_file, "ZMQ_CURVE_SERVER_PUBLIC_KEY", pub)
    _write_key(env_file, "ZMQ_CURVE_SERVER_SECRET_KEY", sec)
    print("ZMQ keys already configured in .env. Skipping generation.")
    return

  print("ZMQ keys missing or invalid. Generating new keypair...")
  public_key, secret_key = zmq.curve_keypair()
  pub_z85 = public_key.decode("ascii")
  sec_z85 = secret_key.decode("ascii")

  # Write in-place — avoids os.replace() which fails on Docker bind mounts
  _write_key(env_file, "ZMQ_CURVE_ENABLED", "true")
  _write_key(env_file, "ZMQ_CURVE_SERVER_PUBLIC_KEY", pub_z85)
  _write_key(env_file, "ZMQ_CURVE_SERVER_SECRET_KEY", sec_z85)

  print("New ZMQ CURVE keypair written to .env")
  print(
    f">>> Copy this public key to every Worker's ZMQ_CURVE_SERVER_PUBLIC_KEY: {pub_z85}"
  )


if __name__ == "__main__":
  ensure_keys()
