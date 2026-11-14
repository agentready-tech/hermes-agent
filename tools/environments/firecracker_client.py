"""Local stdio client for the Firecracker worker's Unix-socket protocol.

A length-prefixed JSON request precedes the reply. On accepted exec, the client
half-closes raw stdin at EOF. The worker replies with stdout (1), stderr (2) and
a final one-byte exit code (3), each framed by a network-order channel and size.
"""

import argparse
import json
import os
import socket
import struct
import sys
import threading

MAX_FRAME = 1024 * 1024
HEADER = struct.Struct("!BI")


def read_exact(connection: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise EOFError("Worker closed the connection")
        data.extend(chunk)
    return bytes(data)


def exchange(connection: socket.socket, request: dict) -> dict:
    data = json.dumps(request, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("Worker request too large")
    connection.sendall(struct.pack("!I", len(data)) + data)
    length = struct.unpack("!I", read_exact(connection, 4))[0]
    if not 0 < length <= MAX_FRAME:
        raise ValueError("Invalid worker reply")
    reply = json.loads(read_exact(connection, length))
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise ValueError("Worker rejected the request")
    return reply


def upload(connection: socket.socket) -> None:
    try:
        while chunk := os.read(0, 65536):
            connection.sendall(chunk)
        connection.shutdown(socket.SHUT_WR)
    except OSError:
        return


def relay(connection: socket.socket) -> int:
    while True:
        channel, length = HEADER.unpack(read_exact(connection, HEADER.size))
        if length > MAX_FRAME or channel not in (1, 2, 3) or (channel == 3 and length != 1):
            raise ValueError("Invalid worker stream")
        data = read_exact(connection, length)
        if channel == 3:
            return data[0]
        output = sys.stdout.buffer if channel == 1 else sys.stderr.buffer
        output.write(data)
        output.flush()


def _split_argv(argv):
    if "--" not in argv:
        return argv, False, []
    index = argv.index("--")
    return argv[:index], True, argv[index + 1:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket_path")
    parser.add_argument("generation")
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--user")
    flags, separator, command = _split_argv(sys.argv[1:])
    if not separator or not command:
        parser.error("Expected a command after --")
    args = parser.parse_args(flags)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(25)
            connection.connect(args.socket_path)
            exchange(connection, {"operation": "exec", "generation": args.generation,
                                  "user": args.user, "argv": command})
            connection.settimeout(None)
            if args.stdin:
                threading.Thread(target=upload, args=(connection,), daemon=True).start()
            else:
                connection.shutdown(socket.SHUT_WR)
            return relay(connection)
    except (OSError, EOFError, ValueError):
        print("Firecracker command transport unavailable", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
