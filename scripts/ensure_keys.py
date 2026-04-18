#!/usr/bin/env python
import os
import sys
from pathlib import Path
import zmq

def ensure_keys():
    env_file = Path(".env")
    
    # If .env doesn't exist, we can't really do much unless we want to create it
    # But usually it should exist (even if empty) if mounted.
    if not env_file.exists():
        print(f"Creating missing {env_file}")
        env_file.touch()

    content = env_file.read_text()
    
    # Check if keys are already set and not placeholders
    has_pub = "ZMQ_CURVE_SERVER_PUBLIC_KEY" in content and '<40-char' not in content
    has_sec = "ZMQ_CURVE_SERVER_SECRET_KEY" in content and '<40-char' not in content
    
    # Also check if they are empty strings
    is_empty_pub = 'ZMQ_CURVE_SERVER_PUBLIC_KEY=""' in content or "ZMQ_CURVE_SERVER_PUBLIC_KEY=''" in content
    is_empty_sec = 'ZMQ_CURVE_SERVER_SECRET_KEY=""' in content or "ZMQ_CURVE_SERVER_SECRET_KEY=''" in content

    if has_pub and has_sec and not is_empty_pub and not is_empty_sec:
        print("ZMQ keys already configured in .env. Skipping generation.")
        return

    print("ZMQ keys missing or placeholder found. Generating new keypair...")
    public_key, secret_key = zmq.curve_keypair()
    pub_z85 = public_key.decode("ascii")
    sec_z85 = secret_key.decode("ascii")

    # If keys exist as placeholders or empty, we should replace them. 
    # For simplicity, we'll just append them at the end if they are missing, 
    # but a better way is to check if we need to replace.
    
    # Let's just append for now as it's the most common request
    new_lines = [
        "",
        "# -- Automatically generated ZeroMQ CURVE keypair --",
        "ZMQ_CURVE_ENABLED=true",
        f'ZMQ_CURVE_SERVER_PUBLIC_KEY="{pub_z85}"',
        f'ZMQ_CURVE_SERVER_SECRET_KEY="{sec_z85}"',
        ""
    ]
    
    with open(env_file, "a") as f:
        f.write("\n".join(new_lines))
    
    print("New ZMQ keys appended to .env")

if __name__ == "__main__":
    ensure_keys()
