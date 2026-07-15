#!/usr/bin/env python3
"""TCP black hole: accepts connections and never sends a byte.

Models the silent-stall failure mode (half-open connection, no RST) that
wedges deadline-less HTTP calls forever. Listens on 127.0.0.1:19999 by
default; pass a port as the only argument to override.
"""
import socket
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 19999
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen(64)
connections = []
while True:
    conn, _ = server.accept()
    connections.append(conn)  # keep the connection open, never respond
