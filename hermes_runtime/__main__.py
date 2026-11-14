"""Managed process entrypoint shipped in the immutable fork image."""

import sys
from pathlib import Path
from hermes_runtime.adapters.hermes import activate


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--validate":
        from hermes_runtime import VERSION

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

    home = Path(get_hermes_home())
    if sys.argv[1:4] == ["--shared-profile-host", "gateway", "run"]:
        from gateway.config import load_gateway_config
        from hermes_runtime.adapters.hermes import activate_profile_host

        if not load_gateway_config().multiplex_profiles:
            raise ValueError("Shared profile host requires gateway.multiplex_profiles")
        activate_profile_host(home)
        del sys.argv[1]
    else:
        activate(home)
        from hermes_runtime.adapters.state import ensure_home_session

        ensure_home_session()
        if sys.argv[1:] == ["--ensure-home"]:
            return
    from hermes_cli.main import main as hermes_main

    hermes_main()


if __name__ == "__main__":
    main()
