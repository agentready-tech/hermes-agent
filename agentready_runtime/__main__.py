"""Managed process entrypoint shipped in the immutable fork image."""

import sys
from pathlib import Path
from agentready_runtime.adapters.hermes import activate


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--validate":
        from agentready_runtime import VERSION

        if sys.argv[2] != VERSION:
            raise RuntimeError(
                f"Managed runtime mismatch: expected {sys.argv[2]}, image has {VERSION}"
            )
        # Validate the image and config before replacing a running container.
        # No activation or home-session mutation in this validation process.
        sys.argv = [sys.argv[0], "config", "check"]
        from hermes_cli.main import main as hermes_main

        hermes_main()
        return
    from hermes_constants import get_hermes_home

    activate(Path(get_hermes_home()))
    from agentready_runtime.adapters.state import ensure_home_session

    ensure_home_session()
    if sys.argv[1:] == ["--ensure-home"]:
        return
    from hermes_cli.main import main as hermes_main

    hermes_main()


if __name__ == "__main__":
    main()
