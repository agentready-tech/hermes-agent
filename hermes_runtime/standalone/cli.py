"""Standalone operator commands; native Hermes owns settings, tools and history."""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path


def parser():
    result = argparse.ArgumentParser(prog="hermes-autonomous")
    result.add_argument("--home", type=Path, help="Native HERMES_HOME (default: active Hermes profile)")
    result.add_argument("--deployment", type=Path, help="Host deployment records and backups, outside HERMES_HOME")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Add local defaults; preserve existing settings and data")
    run = commands.add_parser("run", help="Run native dashboard, gateway, event recovery and optional Gmail")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    commands.add_parser("status", help="Check owner, all configured children and native HTTP health")
    event = commands.add_parser("event", help="Durably admit external data into the main session")
    event.add_argument("--source", required=True)
    event.add_argument("--id", required=True, dest="event_id")
    event.add_argument("text", nargs="?", help="Event text; stdin if omitted")
    schedule = commands.add_parser("schedule", help="Create a native prompt schedule targeting the main inbox")
    schedule.add_argument("--when", required=True)
    schedule.add_argument("--name")
    schedule.add_argument("prompt", nargs="?", help="Prompt text; stdin if omitted")
    commands.add_parser("gmail-worker", help=argparse.SUPPRESS)
    service = commands.add_parser("install-service", help="Install a Linux systemd user service")
    service.add_argument("--enable", action="store_true", help="Enable and start the installed service")
    commands.add_parser("service-run", help=argparse.SUPPRESS)
    commands.add_parser("desktop-session", help=argparse.SUPPRESS)
    up = commands.add_parser("up", help="Start a host-managed Docker deployment")
    up.add_argument("--image", required=True)
    up.add_argument("--port", type=int, default=9119)
    up.add_argument("--mount", action="append", default=[], help="Additional persistent HOST_PATH:CONTAINER_PATH")
    commands.add_parser("backup", help="Stop writers, snapshot complete data, then restart")
    update = commands.add_parser("upgrade", help="Snapshot, replace image, check health; restore on failure")
    update.add_argument("--image", required=True)
    rollback = commands.add_parser("rollback", help="Restore matching image and complete data")
    rollback.add_argument("snapshot", type=Path)
    commands.add_parser("recover", help="Recover a host update interrupted by shutdown")
    return result


def mounts(values):
    parsed = []
    for value in values:
        source, separator, target = value.partition(":")
        if not separator or not Path(target).is_absolute():
            raise ValueError("--mount expects HOST_PATH:ABSOLUTE_CONTAINER_PATH")
        parsed.append({"source": str(Path(source).expanduser().resolve()), "target": target})
    return parsed


def remote_command(args, home):
    """All live SQLite readers/writers stay inside the container's OS namespace."""
    if args.command not in {"event", "schedule", "status"}:
        return None
    directory = args.deployment or home.with_name(home.name + "-deployment")
    if not (directory / "deployment.json").exists():
        return None
    from hermes_runtime.standalone.deployment import Deployment
    operator = Deployment(directory)
    if args.command != "status" and (directory / "transition.json").exists():
        raise RuntimeError("Deployment transition in progress; retry admission after upgrade/recover finishes")
    value = operator.read()
    operator.docker.owned(value)
    options, content = [], None
    if args.command == "event":
        options = ["--source", args.source, "--id", args.event_id]
        content = args.text if args.text is not None else sys.stdin.read()
    if args.command == "schedule":
        options = ["--when", args.when, *(["--name", args.name] if args.name else [])]
        content = args.prompt if args.prompt is not None else sys.stdin.read()
    return json.loads(operator.docker.call("exec", "-i", value["name"], "/opt/hermes/.venv/bin/hermes-autonomous",
        args.command, *options, input=content))


def execute(args, home):
    from hermes_runtime.standalone.config import activate_home, initialize
    if args.command in {"up", "backup", "upgrade", "rollback", "recover"}:
        from hermes_runtime.standalone.deployment import Deployment
        operator = Deployment(args.deployment or home.with_name(home.name + "-deployment"))
        dispatch = {"up": lambda: operator.up(home, args.image, args.port, mounts(args.mount)),
                    "backup": operator.backup, "upgrade": lambda: operator.upgrade(args.image),
                    "rollback": lambda: operator.rollback(args.snapshot), "recover": operator.recover}
        return dispatch[args.command]()
    remote = remote_command(args, home)
    if remote is not None:
        return remote
    if args.command == "init":
        initialize(home)
        return {"home": str(home), "config": str(home / "config.yaml"), "secrets": str(home / ".env")}
    if args.command == "run":
        directory = args.deployment or home.with_name(home.name + "-deployment")
        if (directory / "deployment.json").exists():
            raise ValueError("This home is owned by a Docker deployment; start its existing container instead of a second native owner")
        from hermes_runtime.standalone.supervisor import run
        return run(home, host=args.host, port=args.port)
    if args.command == "status":
        from hermes_runtime.standalone.supervisor import status
        value = status(home)
        print(json.dumps(value, ensure_ascii=False))
        raise SystemExit(0 if value["ok"] else 1)
    if args.command == "install-service":
        from hermes_runtime.standalone.service import install_service
        return install_service(home, args.enable)
    if args.command in {"service-run", "desktop-session"}:
        from hermes_runtime.standalone.desktop_session import run_service, capture_session
        return run_service(home) if args.command == "service-run" else capture_session(home)
    if args.command == "gmail-worker":
        from hermes_runtime.standalone.gmail import worker
        return worker(home)
    activate_home(home)
    if args.command == "event":
        from hermes_runtime.standalone.events import enqueue
        return enqueue(args.text if args.text is not None else sys.stdin.read(), args.source, args.event_id)
    if args.command == "schedule":
        from hermes_runtime.standalone.events import schedule
        return schedule(args.prompt if args.prompt is not None else sys.stdin.read(), args.when, args.name)
    raise ValueError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    if args.home:
        os.environ["HERMES_HOME"] = str(args.home.expanduser().resolve())
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        value = execute(args, home)
        if value is not None:
            print(json.dumps(value, ensure_ascii=False, default=str))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"hermes-autonomous: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
