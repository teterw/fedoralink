"""Entry point: ``python3 -m fedoralink``.

With no subcommand it runs the daemon, which is how systemd starts it.
The subcommands are a thin client over the same D-Bus interface the shell
extension uses — the daemon owns the one Bluetooth link, so nothing here
does the work itself.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import cli
from .daemon import Daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fedoralink",
        description="Bluetooth link between Fedora and Android.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log protocol-level detail"
    )

    # Optional on purpose: `fedoralink` and `python3 -m fedoralink -v` have
    # always meant "run the daemon", and systemd's unit relies on it.
    sub = parser.add_subparsers(dest="command")

    send = sub.add_parser("send", help="send a file to the phone")
    send.add_argument("paths", nargs="+", metavar="FILE")

    sub.add_parser("cancel", help="cancel the transfer in flight")
    sub.add_parser("status", help="show the link state")
    sub.add_parser("devices", help="list enrolled phones")
    sub.add_parser("forget", help="revoke every enrolled phone")
    sub.add_parser("ping", help="ring the phone")

    return parser


def run_command(args: argparse.Namespace) -> int:
    try:
        if args.command == "send":
            return cli.send(args.paths)
        if args.command == "cancel":
            return cli.cancel()
        if args.command == "status":
            return cli.status()
        if args.command == "devices":
            return cli.devices()
        if args.command == "forget":
            return cli.forget()
        if args.command == "ping":
            return cli.ping()
    except cli.DaemonUnavailable as exc:
        print(f"fedoralink: {exc}", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    args = build_parser().parse_args()

    if args.command is not None:
        return run_command(args)

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
