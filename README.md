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

## What it doesn't do

No screen mirroring, no photo sync. Bluetooth RFCOMM carries roughly
**100–250 KB/s** — ample for notifications and clipboard text, hopeless for
anything bulk. A 100 MB video would take about eleven minutes.

File transfer was on that list for the same reason, and no longer is. The
speed limit is real, so rather than refuse the feature outright the plan is a
LAN transport for when both devices are on the same network, with Bluetooth
staying the fallback — that keeps the "works without a network" property that
is the point of this project. Neither is built yet; see
[Beyond Bluetooth](ROADMAP.md#milestone-4--beyond-bluetooth). Until then, use
KDE Connect for bulk transfers.

## Roadmap

| Milestone | Feature | Status |
|---|---|---|
| 1 | Stop Ringing | In progress |
| 1 | Fix `Ringer.stop()` not cancelling vibration | In progress |
| 2 | Handshake authentication | Planned |
| 2 | Tests for the protocol layer | Planned |
| 3 | Low-battery warning, ring-the-PC, lock-on-leave | Planned |
| 3 | Notification replies | Planned |
| 3 | Media control | Idea |
| 4 | LAN/TCP transport | Planned |
| 4 | File transfer | Planned |

Full plan, with the approach and acceptance criteria for each item, is in
[ROADMAP.md](ROADMAP.md).

> **If you change the project, update the progress in the same commit.**
> Flip the status of what you worked on in
> [ROADMAP.md](ROADMAP.md), tick the acceptance criteria you satisfied, and
> keep the table above in agreement with it. A roadmap that lags behind the
> code tells people things that aren't true — treat the status line as part
> of the change, not paperwork after it.

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
  permission lifts this — `READ_CLIPBOARD_IN_BACKGROUND` is signature-level,
  so not even ADB can grant it. Phone → PC therefore has to be
  user-initiated, by one of:
  - **Share → FedoraLink** from any app. The best route: shared text arrives
    in the Intent, so no clipboard read happens and the restriction never
    applies.
  - the Quick Settings tile, or the button in the app.
  - copying while the FedoraLink app is open, which syncs automatically —
    legal only because the app holds focus.

  PC → phone is always automatic.
- **The persistent notification on the phone is mandatory.** Android kills
  background processes holding sockets; a foreground service is the only way
  to stay connected, and it must show a notification. It's set to minimum
  importance so it sits collapsed at the bottom of the shade.
- **Desktop clipboard reads happen in the shell, not the daemon.** Wayland
  has no clipboard-change signal, and `wl-paste --watch` needs the
  `data-control` protocol, which Mutter still does not implement (rechecked
  on Mutter 50). Polling `wl-paste` is not a viable substitute: without
  `data-control` every run has to map a real `xdg_toplevel` to take focus,
  so a timer-driven poll put a `wl-clipboard` window in the dock and took it
  away again every two seconds.

  So the GNOME Shell extension does the clipboard I/O. The shell *is* the
  compositor — it reads and writes the selection with no client, no window
  and no focus requirement, and Mutter gives it a real `owner-changed`
  signal, which makes desktop → phone sync event-driven rather than polled.
  The daemon falls back to `wl-copy` only when the extension isn't running.

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
`Protocol.kt`, then a plugin in `daemon/fedoralink/plugins/`. When you do,
update its entry in [ROADMAP.md](ROADMAP.md) and the summary table under
[Roadmap](#roadmap).

## Uninstall

```
curl -fsSL https://raw.githubusercontent.com/teterw/fedoralink/main/install.sh | bash -s -- --uninstall
```

## License

MIT
