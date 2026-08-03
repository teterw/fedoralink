# FedoraLink

Connect your Android phone to Fedora **over Bluetooth** — no shared Wi-Fi
network, no cloud account, no router involved.

Think Phone Link, but for GNOME, and pure Bluetooth. The toggle lives in the
Quick Settings panel next to Wi-Fi and brightness, the way GSConnect does.

```
┌─────────────────────────────┐
│  Wi-Fi        Bluetooth     │
│  ─────        ─────────     │
│  FedoraLink   Brightness    │   ← the toggle lands here
│  Pixel · 84%                │
└─────────────────────────────┘
```

## What it does

| Feature | Direction |
|---|---|
| Notification mirroring, with two-way dismissal | Phone → PC |
| Clipboard sync | Both |
| Phone battery in Quick Settings | Phone → PC |
| Find My Phone (rings through silent/DND) | PC → Phone |
| Auto-reconnect when the phone comes back in range | — |

## What it deliberately does not do

Bluetooth RFCOMM carries roughly **100–250 KB/s**. That's ample for
notifications and clipboard text, and hopeless for anything else. So there is
no screen mirroring, no photo sync, and no bulk file transfer — those need
Wi-Fi, and adding them would mean giving up the "works without a network"
property that is the entire point of this project.

A 100 MB video would take about eleven minutes. Use KDE Connect for that.

## Install

### Fedora (PC)

```
curl -fsSL https://raw.githubusercontent.com/teterw/fedoralink/main/install.sh | bash
```

Everything lands under `~/.local` — no system files are touched. The only
step that needs `sudo` is installing missing packages, and on a stock Fedora
Workstation there usually aren't any.

**Then log out and back in.** GNOME Shell cannot load a new extension on
Wayland without a fresh session. After logging back in:

```
gnome-extensions enable fedoralink@teterw.github.io
```

### Android (phone)

Download the APK from [Releases](https://github.com/teterw/fedoralink/releases)
and sideload it. Release builds are signed with the project's own key, but
Android will still ask you to allow installs from an unknown source, and Play
Protect may warn that the app isn't commonly downloaded — **More details →
Install anyway**.

Then unlock the one thing Android reserves for a human:

**Settings → Apps → FedoraLink → ⋮ → Allow restricted settings**

Sideloaded apps cannot be granted notification access until that is done. The
toggle will silently refuse to stick otherwise, which looks like a bug in the
app but isn't.

### Pair them

1. GNOME Settings → Bluetooth → pair your phone the normal way.
2. Open FedoraLink on the phone, pick your PC, grant permissions.
3. Flip **Enable link**.

Notification access has to be granted separately, in Android's own Settings —
there is no runtime permission dialog for it. The app has a button that takes
you to the right screen.

## Architecture

```
  Android                        Fedora
 ┌──────────────────┐          ┌──────────────────────────┐
 │ LinkService      │          │ fedoralink daemon        │
 │  (foreground)    │◄─RFCOMM─►│  (systemd --user)        │
 │ NotificationRelay│  NDJSON  │  plugins/                │
 └──────────────────┘          └───────────┬──────────────┘
                                           │ D-Bus
                                           │ org.fedoralink.Daemon
                               ┌───────────▼──────────────┐
                               │ GNOME Shell extension    │
                               │  Quick Settings toggle   │
                               └──────────────────────────┘
```

The Bluetooth link lives in the daemon, not the extension. GNOME reloads
extensions on lock, unlock, and monitor changes — keeping the socket out of
that process means none of it drops your connection.

The wire protocol is newline-delimited JSON, so you can watch it with a
terminal and add a feature by adding a packet type.

## Known constraints

These are platform limits, not bugs, and they shaped the design:

- **Android will not let a background app read the clipboard.** Since
  Android 10 only a focused app or the active keyboard may read it, and no
  permission lifts this. Phone → PC clipboard therefore has to be
  user-initiated: use the Quick Settings tile or the button in the app. PC →
  phone works automatically.
- **The persistent notification on the phone is mandatory.** Android kills
  background processes holding sockets; a foreground service is the only way
  to stay connected, and it must show a notification. It's set to minimum
  importance so it sits collapsed at the bottom of the shade.
- **Clipboard polling on the desktop.** Wayland has no clipboard-change
  signal, and `wl-paste --watch` depends on a protocol Mutter has been
  inconsistent about. The daemon polls every 2 seconds instead, and only
  while a phone is connected.

## Development

```
# Run the daemon in the foreground with protocol logging
cd daemon && python3 -m fedoralink -v

# Watch what the installed service is doing
journalctl --user -u fedoralink -f

# Inspect daemon state
busctl --user introspect org.fedoralink.Daemon /org/fedoralink/Daemon

# Build the APK (needs Android SDK + JDK 21)
cd android && gradle assembleDebug
```

Adding a feature means adding a packet type to both `protocol.py` and
`Protocol.kt`, then a plugin in `daemon/fedoralink/plugins/`.

## Uninstall

```
curl -fsSL https://raw.githubusercontent.com/teterw/fedoralink/main/install.sh | bash -s -- --uninstall
```

## License

MIT
