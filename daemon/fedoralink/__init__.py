"""FedoraLink — Bluetooth link between Fedora and Android."""

import gi

# Declared once, here, because it must run before anything imports
# gi.repository. Doing it in every module instead forces imports below
# the call and litters the codebase with lint suppressions.
gi.require_version("Gio", "2.0")

__version__ = "0.1.0"
