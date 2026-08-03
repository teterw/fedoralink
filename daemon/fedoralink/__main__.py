"""Entry point: ``python3 -m fedoralink``."""

from __future__ import annotations

import argparse
import logging
import sys

from .daemon import Daemon


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="fedoralink",
        description="Bluetooth link between Fedora and Android.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log protocol-level detail"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        # systemd --user already timestamps the journal, so don't duplicate it.
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        Daemon().run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.exception("daemon exited unexpectedly")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
