# FedoraLink Roadmap

What's built, what's next, and what each item actually involves.

> ## Keeping this file honest
>
> **If you change the project, update the progress here in the same commit.**
>
> That means: flip the status of whatever you worked on, tick the acceptance
> criteria you satisfied, and add a line to **Notes** if you learned something
> that changes the plan. Then update the summary table in
> [README.md](README.md#roadmap) so the two agree.
>
> A roadmap that lags behind the code is worse than no roadmap — it tells
> people things that aren't true. Treat the status line as part of the change,
> not paperwork after it.
>
> Shipping something that isn't listed here? Add it, with a status of
> **Shipped**, so the record is complete.

## Status legend

| | Meaning |
|---|---|
| **Shipped** | Merged, works on a real phone, documented in the README |
| **In progress** | Someone is actively writing it |
| **Designed** | The approach is settled and written down below; nobody has started |
| **Planned** | Agreed it should exist; the approach is still open |
| **Idea** | Worth considering, not committed to |

---

## Shipped

| Feature | Direction | Lives in |
|---|---|---|
| Notification mirroring, with two-way dismissal | Phone → PC | `plugins/notification.py`, `NotificationRelay.kt` |
| Clipboard sync | Both | `plugins/clipboard.py`, `ClipboardBridge.kt`, `extension.js` |
| Phone battery in Quick Settings | Phone → PC | `plugins/battery.py`, `BatteryReporter.kt` |
| Find My Phone (rings through silent/DND) | PC → Phone | `plugins/ping.py`, `Ringer.kt` |
| Auto-reconnect when the phone comes back in range | — | `daemon.py`, `LinkManager.kt` |
| CI Android job fixed (`setup-android` v3 → v4) | — | `.github/workflows/` |

---

## Milestone 1 — Finish Find My Phone

### Stop Ringing — **In progress**

A second Quick Settings item that silences the phone before the 15-second
timeout expires. It appears under **Find My Phone** once you've pressed that,
and disappears when the ring ends.

**Approach.** No new packet type: `ring_phone()` already sends
`fedoralink.ping` with `{"ring": true}`, so stop is `{"ring": false}` on the
same type. An older phone build ignores it — its handler only acts when `ring`
is true — so this degrades instead of breaking, and `PROTOCOL_VERSION` stays
at 1.

- `plugins/ping.py` — add `stop_ringing()` (named around `Plugin.stop()`)
- `dbus_service.py` — `StopRinging` in `INTROSPECTION` and `_handle_method_call`
- `extension.js` — mirror the method in `DaemonInterface`; add `_stopRingItem`
  after `_pingItem`, hidden by default; **Find My Phone** shows it and arms a
  15s `GLib.timeout_add`; clear the timer in `destroy()` and `_setUnavailable()`
- `LinkService.kt` — the ping handler becomes ring-or-stop

**Acceptance criteria**

- [ ] Pressing **Stop Ringing** silences the phone immediately — ringtone *and*
      vibration
- [ ] The item is not visible when nothing is ringing
- [ ] The item disappears on its own after the ring times out
- [ ] Disconnecting mid-ring doesn't leave the item stranded in the menu
- [ ] An old phone build receiving `{"ring": false}` does nothing and stays
      connected

**Released in v0.4.0** (2026-09-18) — but the status stays **In progress**,
not **Shipped**: the legend reserves that for what has been seen working on a
real phone, and none of the criteria above have been checked yet. Whoever
tests it first should tick them and flip both statuses.

**Known limitation.** If the link drops mid-ring and comes back inside the 15
seconds, the item does not reappear — the desktop has no way to know the phone
is still ringing. The phone's own timer still silences it. Fixing this properly
needs the phone to report ring state, which isn't worth a packet type yet.

### Fix `Ringer.stop()` not cancelling vibration — **In progress**

`Ringer.vibrate()` starts a waveform with repeat index `0`, so it buzzes until
something cancels it, and that cancel is its own `postDelayed` callback.
`Ringer.stop()` only removes `stopRunnable`, so it never touches the vibrator.

Nobody notices today because stop and the vibration cancel both land at 15
seconds. The moment you can stop early, the ringtone goes quiet and the phone
keeps buzzing for the remainder. Ships with Stop Ringing, since that's what
exposes it.

- [x] Hold the vibrator cancel in a named `Runnable`, like `stopRunnable`
- [x] Cancel the vibrator inside `stop()`

---

## Milestone 2 — Make the link trustworthy

### Handshake authentication — **In progress**

Today the identity handshake exchanges `deviceName`, `deviceType`,
`protocolVersion` and `capabilities`, and nothing else. All trust rests on
Bluetooth pairing, which means anything paired with the PC gets the full
clipboard stream and every notification that crosses the link. This is the
largest real gap in the project.

**Direction** (approach still open): generate a shared secret at first
connection, display it as a short code on both ends for the human to confirm,
persist it on each side, and check it in the handshake before any plugin sees
a packet. An unauthenticated peer gets the handshake and nothing else.

**Decisions taken** (the open questions, answered)

- **Storage.** `~/.local/share/fedoralink/devices.json` at `0600`, written
  atomically via temp-file-and-rename so a crash mid-write can't leave a
  truncated store that locks out every device. Phone side is
  `EncryptedSharedPreferences`, falling back to ordinary private prefs with a
  log line if the Keystore is unavailable.
- **Version.** `PROTOCOL_VERSION` is 2 and `MIN_PROTOCOL_VERSION` is 2 — an
  old peer is refused, not degraded. A version 1 peer has no secret, so
  accepting it is the exact thing this prevents. The log says "update the app"
  rather than dropping silently.
- **Confirmation.** Desktop notification with Approve/Reject, because that's
  where the human and the trust store both are. The phone only displays the
  code for comparison, which kept the phone side to a status line.

**Acceptance criteria**

- [x] A paired-but-unauthorised device cannot read the clipboard or receive
      notifications — gated both inbound and outbound, 30 tests
- [x] The confirmation code is shown on both devices and must match — the
      phone refuses an offer whose code doesn't match the secret's fingerprint
- [x] The secret survives a daemon restart and a phone reboot
- [x] Revoking a device is possible without re-pairing Bluetooth —
      `ForgetDevices` over D-Bus, or "Forget paired PCs" in the app

Unverified on hardware: no phone here, so the round trip has never actually
run. The logic is covered by tests on the Python side; the Kotlin half has
none.

### Tests for the protocol layer — **Shipped**

There is no test suite. `PacketReader` is the right first target: pure Python,
no GLib, and its whole job is the cases that are easy to get wrong and
catastrophic when broken.

- [x] A packet split across two reads reassembles
- [x] A packet split mid-multibyte-character doesn't produce mojibake
- [x] An oversize line with no newline raises `ProtocolError` and clears the
      buffer
- [x] A malformed packet raises but leaves the stream framed and usable
- [x] Blank lines are skipped
- [x] `pytest` runs in CI alongside the existing `compileall` and `ruff` steps

26 tests in `daemon/tests/test_protocol.py`. Mutation-checked: removing the
oversize buffer clear and flipping `ensure_ascii` both turn the suite red, so
it is pinning real behaviour rather than passing vacuously.

---

## Milestone 3 — Things the data already supports

### Quick wins bundle — **In progress**

Three small features where the plumbing is already in place.

**Low-battery desktop warning.** `BatteryLevel` already reaches the shell over
D-Bus. A threshold check in `plugins/battery.py` calling
`self.daemon.notifications.show_local(...)` is most of it.

- [x] Notifies once when the phone drops below the threshold, not on every packet
- [x] Re-arms after the phone charges back up
- [x] Threshold is configurable

**Ring the PC from the phone.** The inverse of Find My Phone is half-built:
`PingPlugin.on_packet` already shows a desktop notification when the phone
pings, it just doesn't make a sound.

- [x] Audible alert on the desktop, not just a notification
- [x] A button in the Android app that triggers it
- [ ] Works when the desktop is locked — unverified, needs a real session

**Lock the desktop when the phone leaves range.** The daemon already gets
`on_disconnected`; this calls the screensaver's D-Bus `Lock`.

- [x] Off by default, behind a setting — a flaky link that locks your screen
      is infuriating
- [x] A grace period, so a momentary drop doesn't lock you out mid-sentence

### Kotlin `Protocol.Reader` tests — **Planned**

Discovered while writing the Python suite. The Python reader buffers raw bytes
and only decodes complete lines, so it cannot mojibake a split multibyte
character — the property holds structurally. `Protocol.Reader` in Kotlin keeps
the same guarantee through explicit `lastIndexOf('\n')` logic instead, which a
future edit could quietly break, and it has no tests.

Needs a JVM unit-test source set (`src/test/kotlin`), a JUnit dependency, and
a `gradle test` step in CI.

- [ ] Split reads, byte-by-byte feeds, and a split multibyte character
- [ ] Oversize line resets both buffers
- [ ] Malformed packet is skipped without dropping the stream
- [ ] `gradle test` runs in CI

### Notification replies — **Planned**

Answer a message from the desktop notification instead of picking up the
phone. `NOTIFICATION_ACTION` already exists and carries only `dismiss`, so the
packet type and the key mapping in `plugins/notification.py` are already there.

Needs Android's `RemoteInput` on the phone side and a text entry on the
freedesktop notification, which not every notification daemon supports — GNOME
Shell does.

- [ ] Replying from the desktop delivers the message through the originating app
- [ ] Notifications without a reply action don't grow a dead text box
- [ ] A failed reply says so, rather than silently dropping the text

### Media control — **Idea**

Desktop MPRIS keys from the phone, or phone playback from Quick Settings.
Tiny packets, so it sits comfortably within the Bluetooth budget. Unscoped —
decide which direction matters first.

---

## Milestone 4 — Beyond Bluetooth

**This milestone revises a stated design principle.** The README said
FedoraLink deliberately does no file transfer, because RFCOMM's ~100–250 KB/s
makes it hopeless. That number hasn't changed. What changes is the conclusion:
instead of refusing the feature, add a faster transport and use Bluetooth as
the fallback that keeps the "works without a network" promise intact.

Do these in order — file transfer over RFCOMM alone would be a bad first
impression of the feature.

### LAN/TCP transport — **Planned**

A second transport used when both devices are on the same network, falling
back to Bluetooth when they aren't.

`RfcommTransport` is a concrete class, not an interface, so the first real
task is extracting a transport abstraction. `Connection` is already separable
enough to reuse — it deals in packets and a file descriptor, not in Bluetooth.

**Open questions**

- Discovery: mDNS, or reuse the Bluetooth link to exchange an address?
- Does this need TLS, or does the Milestone 2 shared secret cover it? (A LAN
  is a far more hostile place than an RFCOMM pairing — assume it needs more)
- Switching transports mid-session without dropping state

**Acceptance criteria**

- [ ] Same Wi-Fi: connects over TCP, measurably faster than RFCOMM
- [ ] No shared network: falls back to Bluetooth, unprompted
- [ ] Leaving Wi-Fi mid-session doesn't drop the link
- [ ] An attacker on the same network cannot read the stream

### File transfer — **Planned**

Send files both ways, with a chunked packet type and progress reporting.

- [ ] Send from the desktop (Files integration or drag onto the toggle) and
      from the phone (Android share sheet)
- [ ] Progress is visible, and a transfer can be cancelled
- [ ] A dropped link resumes or fails cleanly — never a silent half-file
- [ ] Over Bluetooth, the UI is honest about how slow it will be

---

## Notes

Running log of decisions and discoveries that changed the plan. Newest first.

- **2026-09-23** — Authentication landed. Two design notes worth keeping. The
  fingerprint is a hash of the secret, not a slice of it — the code appears on
  a lock screen, where notifications land, and showing key bytes there would
  hand the secret to anyone glancing at the phone. And `isConnected()` on the
  phone now means connected *and* authenticated: all nine callers meant "can I
  send?", and the answer before the handshake is no, so folding it in beat
  making every caller check twice.
- **2026-09-23** — Quick wins bundle built. Two of the three needed settings,
  so there is now a `config.py` reading `~/.config/fedoralink/config.json` —
  stdlib-only and validated key-by-key, so one typo can't stop the daemon.
  Ring-the-PC needed somewhere to make noise: `alert.py` re-spawns
  `canberra-gtk-play` on a timer, falling back to the notification's
  `sound-name` hint when it isn't installed. Lock-on-leave became
  `plugins/presence.py` — the Plugin base already had connect/disconnect
  hooks, so it needed no new machinery.
- **2026-09-23** — `fedoralink.ping` now carries three meanings, not two: no
  `ring` key is the link test, `ring: true` rings, `ring: false` silences.
  Treating absent and false alike would pop a "Ping from your phone"
  notification while silencing the PC alert. Pinned by a test.
- **2026-09-23** — Protocol tests landed. Mutation-testing them turned up
  something worth recording: the Python `PacketReader` is immune to
  mid-multibyte-character splits *by construction* (it buffers bytes, decodes
  whole lines), so that test can never fail there. Kotlin's `Protocol.Reader`
  maintains the same property through explicit logic, so it is the side where
  a regression is possible — and it has no tests. Added as its own item.
- **2026-09-18** — Released as v0.4.0 with a signed APK. Getting there meant
  fixing CI first: `android-actions/setup-android@v3` defaults to installing
  the `tools` package, which Google has removed from the SDK repository, so
  `sdkmanager` exited 1 and the Android job had been dying before compiling
  anything — on every push, for some time. `release.yml` shared the step, so
  the tag would have produced no APK. Bumped to v4, which drops `tools` from
  its defaults. The green Android job is also the first time this code has
  been compiled at all.
- **2026-09-18** — Stop Ringing and the vibration fix are written. Static
  checks pass (`compileall`, `ruff`, and the extension parses as an ES
  module); the Kotlin is unbuilt locally — no Android SDK on this machine, so
  CI's `assembleDebug` is the first real compile. None of the Stop Ringing
  acceptance criteria are ticked yet: every one of them needs a running shell
  and a real phone to observe.
- **2026-09-18** — Roadmap created. Milestone 4 was agreed as a deliberate
  revision of the README's "no file transfer" stance: the Bluetooth speed
  limit is real, so the answer is a second transport rather than a permanent
  refusal.
- **2026-09-18** — Found that `Ringer.stop()` never cancels vibration. Latent
  today, a visible bug the moment early stop exists.
