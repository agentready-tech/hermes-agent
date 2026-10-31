"""A tenant's native Docker client cannot control another sandbox or the host."""

import asyncio
import copy
import json
from pathlib import Path
import tempfile

import pytest

from hermes_runtime.standalone.docker_api import Policy, Server, daemon_json


@pytest.fixture
def policy():
    own = {"Id": "a" * 64, "Name": "/own", "Image": "sha256:desktop", "Config": {
        "Image": "hermes-sandbox:desktop", "Labels": {"hermes-agent": "1", "hermes-environment": "tenant-one"}},
        "HostConfig": {"Binds": ["/srv/one/workspace:/workspace"], "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges"], "Privileged": False}}
    foreign = copy.deepcopy(own)
    foreign["Id"] = "b" * 64
    foreign["Name"] = "/foreign"
    foreign["Config"]["Labels"]["hermes-environment"] = "tenant-two"
    foreign["HostConfig"]["Binds"] = ["/srv/two/workspace:/workspace"]
    records = {"/containers/own/json": own, "/containers/foreign/json": foreign,
               "/containers/" + own["Id"] + "/json": own,
               "/containers/" + foreign["Id"] + "/json": foreign,
               "/exec/aaa/json": {"ContainerID": own["Id"]},
               "/exec/bbb/json": {"ContainerID": foreign["Id"]}}
    def lookup(path):
        if path not in records:
            raise PermissionError("missing")
        return records[path]
    return Policy({"sandbox": own}, lookup=lookup)


def test_native_client_can_inspect_start_and_exec_its_existing_sandbox(policy):
    for method, target, body in [("GET", "/v1.53/containers/own/json", b""),
                                 ("POST", "/containers/own/start", b""),
                                 ("POST", "/containers/own/exec", b'{"Cmd":["bash"]}'),
                                 ("GET", "/exec/aaa/json", b"")]:
        assert policy.authorize(method, target, body) == "forward"
    assert policy.authorize("POST", "/exec/aaa/start", b"{}") == "stream"


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/containers/foreign/json", b""), ("POST", "/containers/foreign/start", b""),
    ("POST", "/exec/bbb/start", b"{}"), ("POST", "/containers/own/exec", b'{"Privileged":true}'),
    ("POST", "/networks/create", b"{}"), ("POST", "/volumes/create", b"{}"),
    ("POST", "/build", b""), ("POST", "/images/create?fromImage=other", b""),
    ("POST", "/containers/own/archive", b""), ("GET", "http://other/version", b""),
])
def test_foreign_resources_and_daemon_administration_are_denied(policy, method, path, body):
    with pytest.raises(PermissionError):
        policy.authorize(method, path, body)


def test_container_creation_is_host_managed_even_for_an_identical_sandbox(policy):
    original = {"Image": policy.template["Config"]["Image"],
                "Labels": policy.template["Config"]["Labels"], "HostConfig": policy.template["HostConfig"]}
    with pytest.raises(PermissionError):
        policy.authorize("POST", "/containers/create", json.dumps(original).encode())
    for key, value in [("Binds", ["/:/host"]), ("Privileged", True), ("NetworkMode", "host"),
                       ("PidMode", "host"), ("PortBindings", {"80/tcp": [{"HostPort": "80"}]}),
                       ("SecurityOpt", ["seccomp=unconfined"]), ("CapAdd", ["SYS_ADMIN"])]:
        changed = copy.deepcopy(original)
        changed["HostConfig"][key] = value
        with pytest.raises(PermissionError):
            policy.authorize("POST", "/containers/create", json.dumps(changed).encode())


def test_matching_labels_alone_do_not_grant_ownership(policy):
    original = policy.lookup
    forged = copy.deepcopy(policy.template)
    forged["Id"] = "c" * 64
    forged["HostConfig"]["Binds"] = ["/srv/two/workspace:/workspace"]
    policy.lookup = lambda path: forged if path == "/containers/forged/json" else original(path)
    assert not policy.owns("forged")


@pytest.mark.platforms("posix")
def test_http_transport_filters_list_and_rejects_request_smuggling(policy):
    async def exercise(target):
        own = policy.template["Id"]
        original = policy.lookup
        policy.lookup = lambda path: [{"Id": own}, {"Id": "b" * 64}] if path == "/containers/json" else original(path)
        async with await asyncio.start_unix_server(Server(policy, "/missing").handle, str(target)):
            reader, writer = await asyncio.open_unix_connection(str(target))
            writer.write(b"GET /containers/json HTTP/1.1\r\nHost: docker\r\n\r\n")
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 3)
            assert json.loads(data.split(b"\r\n\r\n")[1]) == [{"Id": own}]
            writer.close()
            reader, writer = await asyncio.open_unix_connection(str(target))
            writer.write(b"POST /containers/own/exec HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 20\r\n\r\n")
            await writer.drain()
            assert b"403 Forbidden" in await asyncio.wait_for(reader.read(), 3)
            writer.close()
    # AF_UNIX paths have a small fixed limit, including pytest's directory prefix.
    with tempfile.TemporaryDirectory(prefix="ag-api-", dir="/tmp") as directory:  # no-tmp: short AF_UNIX path
        asyncio.run(exercise(Path(directory) / "guard.sock"))


@pytest.mark.platforms("posix")
def test_exec_upgrade_forwards_stdin_and_output_without_hanging(policy):
    async def exercise(directory):
        daemon, guard = directory / "d.sock", directory / "g.sock"
        async def upstream(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            size = int(next(line.split(b":", 1)[1] for line in header.split(b"\r\n")
                            if line.lower().startswith(b"content-length:")))
            await reader.readexactly(size)
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            await writer.drain()
            incoming = await reader.readexactly(4)
            writer.write(b"proof:" + incoming)
            await writer.drain()
            writer.close()
        async with await asyncio.start_unix_server(upstream, str(daemon)):
            async with await asyncio.start_unix_server(Server(policy, str(daemon)).handle, str(guard)):
                reader, writer = await asyncio.open_unix_connection(str(guard))
                writer.write(b"POST /exec/aaa/start HTTP/1.1\r\nHost: docker\r\nContent-Length: 2\r\nUpgrade: tcp\r\n\r\n{}")
                await writer.drain()
                assert b"101 Switching" in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                writer.write(b"test")
                await writer.drain()
                assert await asyncio.wait_for(reader.read(), 3) == b"proof:test"
                writer.close()
    with tempfile.TemporaryDirectory(prefix="ag-api-", dir="/tmp") as directory:  # no-tmp: short AF_UNIX path
        asyncio.run(exercise(Path(directory)))


@pytest.mark.platforms("posix")
def test_daemon_lookup_uses_a_valid_http_host_over_the_unix_socket():
    async def exercise(path):
        received = []
        async def daemon(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            received.append(header)
            body = b'{"Id":"existing"}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
        async with await asyncio.start_unix_server(daemon, str(path)):
            value = await asyncio.to_thread(daemon_json, "/containers/own/json", daemon=str(path))
            assert value == {"Id": "existing"}
            assert b"Host: docker\r\n" in received[0]
    with tempfile.TemporaryDirectory(prefix="ag-api-", dir="/tmp") as directory:  # no-tmp: short AF_UNIX path
        asyncio.run(exercise(Path(directory) / "d.sock"))
