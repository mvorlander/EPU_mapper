"""Race-free port reservation and explicit launcher readiness notifications."""
from __future__ import annotations

import errno
import json
import os
import socket
import webbrowser

ADDRESS_PREFIX = "[epumapper-address] "
READY_PREFIX = "[epumapper-ready] "


def browser_url(host: str, port: int) -> str:
    host = "127.0.0.1" if host in ("", "0.0.0.0") else "::1" if host == "::" else host
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


def reserve_socket(host: str, port: int, auto_port: bool = False) -> socket.socket:
    """Hold the actual listening socket through scanning and Uvicorn startup.

    A check-then-close approach leaves a race with another launcher. The child
    owns this socket from the outset, including on Windows (no FD inheritance).
    """
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535 (0 selects a free port).")
    family, kind, protocol, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    sock = socket.socket(family, kind, protocol)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(address)
            sock.listen(128)
        except OSError as exc:
            # Windows exclusive listeners can return WSAEACCES rather than
            # WSAEADDRINUSE. An ephemeral port is safe for either failure.
            busy = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10048, 10013)
            if not auto_port or not busy or port == 0:
                raise
            sock.close()
            return reserve_socket(host, 0)
        return sock
    except BaseException:
        sock.close()
        raise


def announce_address(host: str, requested_port: int, sock: socket.socket) -> dict:
    port = sock.getsockname()[1]
    address = {"port": port, "url": browser_url(host, port), "requested_port": requested_port}
    if requested_port and port != requested_port:
        print(f"[launcher] Port {requested_port} is already in use; using {address['url']}. The existing server was left running.", flush=True)
    print(ADDRESS_PREFIX + json.dumps(address), flush=True)
    return address


def run_reserved_server(app, sock: socket.socket, address: dict, open_browser: bool = False) -> None:
    import uvicorn

    class ReservedServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                print(READY_PREFIX + json.dumps(address), flush=True)
                if open_browser:
                    webbrowser.open(address["url"])

    server = ReservedServer(uvicorn.Config(app, log_level="info"))
    server.run(sockets=[sock])
    if not server.started:
        raise SystemExit(1)
