"""Bounded Docker CLI calls shared by the local-only experiment launchers."""
import subprocess


def command(args):
    return subprocess.check_output(args, text=True, timeout=45).strip()
