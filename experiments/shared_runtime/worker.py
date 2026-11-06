"""Local-only shared BotMode host built on the native profile multiplexer."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--profiles', nargs='*', default=[])
    args = parser.parse_args()
    os.environ['HERMES_HOME'] = str(args.home.resolve())
    from experiments.shared_runtime.transport import install
    install()
    from gateway.run import _profile_runtime_scope
    from hermes_runtime.adapters.hermes import activate, activate_profiles
    from hermes_runtime.adapters.state import ensure_home_session
    from gateway.config import Platform, load_gateway_config
    from gateway.session import SessionSource, SessionStore

    homes = [args.home / 'profiles' / name for name in args.profiles] if args.profiles else [args.home]
    if args.profiles:
        activate_profiles(homes)
    else:
        activate(homes[0])
    config = load_gateway_config()
    routes = SessionStore(config.sessions_dir, config)
    for home in homes:
        with _profile_runtime_scope(home):
            ensure_home_session()
            # A fresh bot has no Telegram ingress yet. Reserve its real native
            # main route locally so the authenticated control client can attach.
            routes.get_or_create_session(SessionSource(platform=Platform.TELEGRAM,
                chat_id='prototype-' + home.name, user_id='9001',
                profile=home.name if args.profiles else None))
    routes.close_all_db_handles()
    from gateway.run import main as gateway_main
    import sys
    sys.argv = [sys.argv[0]]
    gateway_main()


if __name__ == '__main__':
    main()
