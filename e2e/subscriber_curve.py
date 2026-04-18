#!/usr/bin/env python
"""
examples/zmq/subscriber_curve.py
─────────────────────────────────
Example ZeroMQ SUB subscriber that connects with CURVE authentication.

Usage
-----
    python examples/zmq/subscriber_curve.py \\
        --host 127.0.0.1 \\
        --port 5555 \\
        --server-pub-key "rq:rM>}U?@Lns47E1%kR.o@n%FcmmsL/@{H8]yf7" \\
        --topic SIGNAL

Requirements
------------
    pip install pyzmq

The server public key is produced by:
    python scripts/generate_curve_keypair.py
"""

from __future__ import annotations

import argparse
import sys

import zmq


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CURVE-authenticated ZeroMQ subscriber example"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Broker host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5555, help="Broker PUB port (default: 5555)")
    parser.add_argument(
        "--server-pub-key",
        required=True,
        metavar="Z85_KEY",
        help="Server (broker) CURVE public key in Z85 encoding (40 ASCII chars)",
    )
    parser.add_argument(
        "--topic",
        default="SIGNAL",
        help="Topic prefix to subscribe to (default: SIGNAL)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)

    # ── Generate a *throw-away* client keypair ──────────────────────
    # In production you would persist a real client keypair and register
    # the client public key with the server's allow-list.
    client_pub, client_sec = zmq.curve_keypair()

    sock.curve_serverkey = args.server_pub_key.encode()  # Z85 bytes
    sock.curve_publickey = client_pub
    sock.curve_secretkey = client_sec

    sock.setsockopt_string(zmq.SUBSCRIBE, args.topic)

    connect_addr = f"tcp://{args.host}:{args.port}"
    sock.connect(connect_addr)
    print(f"[subscriber] Connected (CURVE) to {connect_addr}, topic='{args.topic}'")
    print("[subscriber] Waiting for messages …  (Ctrl-C to stop)\n")

    try:
        while True:
            raw = sock.recv_string()
            if "|" in raw:
                topic, payload = raw.split("|", 1)
                print(f"[{topic}] {payload}")
            else:
                print(f"[raw] {raw}")
    except KeyboardInterrupt:
        print("\n[subscriber] Interrupted — closing.")
    finally:
        sock.close()
        ctx.term()


if __name__ == "__main__":
    main()
