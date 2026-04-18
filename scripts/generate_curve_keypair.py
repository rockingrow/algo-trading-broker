#!/usr/bin/env python
"""
scripts/generate_curve_keypair.py
──────────────────────────────────
One-shot helper that generates a ZeroMQ CURVE key-pair and prints the
values ready to paste into your .env file.

Usage
-----
    python scripts/generate_curve_keypair.py

Output (example)
----------------
    # ── ZeroMQ CURVE keypair (broker / server) ──
    ZMQ_CURVE_ENABLED=true
    ZMQ_CURVE_SERVER_PUBLIC_KEY=rq:rM>}U?@Lns47E1%kR.o@n%FcmmsL/@{H8]yf7
    ZMQ_CURVE_SERVER_SECRET_KEY=JTKVSB%%)wK0E.X)V>+}o?pNmC{O&4W4b!Ni{Lh6

Distribute the public key to every subscriber/client.
Keep the secret key on the broker only.
"""

import zmq


def main() -> None:
  public_key, secret_key = zmq.curve_keypair()
  # curve_keypair() returns bytes; decode to ASCII Z85 strings
  pub_z85 = public_key.decode("ascii")
  sec_z85 = secret_key.decode("ascii")

  print()
  print("# -- ZeroMQ CURVE keypair (broker / server) --")
  print("ZMQ_CURVE_ENABLED=true")
  print(
    f'ZMQ_CURVE_SERVER_PUBLIC_KEY="{pub_z85}" # Distribute the public key to every subscriber/client.'
  )
  print(
    f'ZMQ_CURVE_SERVER_SECRET_KEY="{sec_z85}" # Keep the secret key on the broker ONLY.'
  )
  print()


if __name__ == "__main__":
  main()
