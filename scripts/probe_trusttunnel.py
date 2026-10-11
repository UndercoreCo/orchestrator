#!/usr/bin/env python3
"""Explicit operator probe using an official client in SOCKS mode, never TUN.

Verify the upstream binary signature first. Profiles are temporary/private;
credentials and visited response bodies are never printed. No node mutations.
"""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.infrastructure.drivers.trusttunnel_configuration import configuration


def private_document(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise ValueError("private_export_required")
        return source.read(65537)


def probe(binary, document, transport, expected_ip, timeout):
    adapted = configuration(document, ipv6_available=False, transport=transport)
    # Pick an unused local port. Check the child is alive before using its listener.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="trusttunnel-probe-") as folder:
        path = Path(folder) / "client.toml"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            target.write(
                'loglevel = "error"\nvpn_mode = "general"\nkillswitch_enabled = false\n'
                + adapted.data
                + f'\n[listener.socks]\naddress = "127.0.0.1:{port}"\n'
            )
        child = subprocess.Popen(
            [str(binary), "--config", str(path), "--loglevel", "error"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                if child.poll() is not None:
                    raise ValueError("client_start_failed")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise ValueError("listener_timeout")
                    time.sleep(0.1)
            # Listener readiness precedes endpoint connection in the upstream client.
            time.sleep(2)
            result = {"transport": transport, "tls_verification": True, "checks": {}}
            for name, url in (
                ("egress", "https://api.ipify.org"),
                ("https", "https://example.com"),
            ):
                start = time.monotonic()
                response = subprocess.run(
                    [
                        "curl",
                        "--fail",
                        "--silent",
                        "--max-time",
                        str(timeout),
                        "--noproxy",
                        "",
                        "--proxy",
                        f"socks5h://127.0.0.1:{port}",
                        url,
                    ],
                    capture_output=True,
                    timeout=timeout + 2,
                )
                ok = child.poll() is None and response.returncode == 0
                if name == "egress":
                    ok = ok and response.stdout.strip() == expected_ip.encode()
                result["checks"][name] = {
                    "ok": ok,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                }
                if not ok:
                    break
            result["ok"] = all(item["ok"] for item in result["checks"].values())
            return result
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="SHA256 of the signature-verified binary")
    parser.add_argument(
        "--endpoint-export", type=Path, required=True, help="Private TOML export (0600)"
    )
    parser.add_argument("--expected-egress", required=True)
    parser.add_argument("--transport", choices=["http2", "http3"], required=True)
    parser.add_argument("--timeout", type=int, default=12, choices=range(1, 61))
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-f0-9]{64}", args.sha256):
            raise ValueError("invalid_checksum")
        if args.binary.is_symlink() or not args.binary.is_file():
            raise ValueError("regular_binary_required")
        if hashlib.sha256(args.binary.read_bytes()).hexdigest() != args.sha256:
            raise ValueError("binary_checksum_mismatch")
        expected_ip = str(ipaddress.ip_address(args.expected_egress))
        result = probe(
            args.binary.resolve(),
            private_document(args.endpoint_export),
            args.transport,
            expected_ip,
            args.timeout,
        )
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({"ok": False, "error": "probe_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
