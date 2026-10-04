#!/usr/bin/env python3
"""Send a Wake-on-LAN magic packet. Usage: wol.py <MAC> [targets...]"""
import socket, sys

def packet(mac: str) -> bytes:
    h = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(h) != 12:
        raise SystemExit(f"bad MAC: {mac}")
    return b"\xff" * 6 + bytes.fromhex(h) * 16

def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: wol.py <MAC> [ip:port ...]")
    mac = sys.argv[1]
    targets = sys.argv[2:] or ["255.255.255.255:9"]
    pkt = packet(mac)
    for t in targets:
        ip, _, port = t.partition(":")
        port = int(port or 9)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.sendto(pkt, (ip, port))
            print(f"sent {len(pkt)}B magic packet for {mac} -> {ip}:{port}")
        except OSError as e:
            print(f"FAILED {ip}:{port}: {e}")
        finally:
            s.close()

if __name__ == "__main__":
    main()
