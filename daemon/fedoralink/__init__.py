"""FedoraLink — Bluetooth link between Fedora and Android."""

try:
    import gi
except ImportError:
    # Only the modules that talk to GLib, D-Bus or BlueZ actually need
    # PyGObject. protocol.py and config.py are plain Python, and the test
    # suite imports them through this package, so requiring gi here would
    # make the tests need a full PyGObject install to check byte framing.
    #
    # This hides nothing: a daemon without gi still fails loudly on the
    # first `from gi.repository import ...` in daemon.py.
    pass
else:
    # Declared once, here, because it must run before anything imports
    # gi.repository. Doing it in every module instead forces imports below
    # the call and litters the codebase with lint suppressions.
    gi.require_version("Gio", "2.0")

__version__ = "0.1.0"
