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
| **Built** | Implemented, tested where testable, CI-green — but never run on real hardware |
| **In progress** | Someone is actively writing it |
| **Designed** | The approach is settled and written down below; nobody has started |
| **Planned** | Agreed it should exist; the approach is still open |
| **Idea** | Worth considering, not committed to |

**Built is not Shipped, and the gap is the point.** Most of this roadmap was
implemented without a phone or an Android SDK to hand. The code compiles and the
tests pass; that is not the same as knowing it works. Anything still marked
**Built** is waiting on someone to install it and use it, at which point the
acceptance criteria can be ticked and the status moved.

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
| `fedoralink` CLI (`status`, `send`, `ping`, `devices`…) | — | `cli.py` |
| Keep phone audio on the phone (`connect_phone_audio`, off) | — | `plugins/audio.py` |

---

## Milestone 1 — Finish Find My Phone

### Stop Ringing — **Built**

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

**Released in v0.4.0** (2026-09-18), so this one is in users' hands — but the
criteria above are still unticked, because nobody has confirmed the button
actually silences a ringing phone. Whoever tests it first should tick them and
move it to **Shipped**.

**Known limitation.** If the link drops mid-ring and comes back inside the 15
seconds, the item does not reappear — the desktop has no way to know the phone
is still ringing. The phone's own timer still silences it. Fixing this properly
needs the phone to report ring state, which isn't worth a packet type yet.

### Fix `Ringer.stop()` not cancelling vibration — **Built**

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

### Handshake authentication — **Built**

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

### Quick wins bundle — **Built**

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

### Kotlin `Protocol.Reader` tests — **Shipped**

Discovered while writing the Python suite. The Python reader buffers raw bytes
and only decodes complete lines, so it cannot mojibake a split multibyte
character — the property holds structurally. `Protocol.Reader` in Kotlin keeps
the same guarantee through explicit `lastIndexOf('\n')` logic instead, which a
future edit could quietly break, and it has no tests.

Needs a JVM unit-test source set (`src/test/kotlin`), a JUnit dependency, and
a `gradle test` step in CI.

- [x] Split reads, byte-by-byte feeds, and a split multibyte character
- [x] Oversize line resets both buffers
- [x] Malformed packet is skipped without dropping the stream
- [x] `gradle test` runs in CI

21 tests in `android/app/src/test/`, which also cover the crypto: HKDF against
RFC 5869, and opening ciphertext the Python side produced. These run on a plain
JVM, which is why `Crypto.kt` and `LanSession.kt` use `java.util.Base64` rather
than Android's.

### Notification replies — **Built**

Answer a message from the desktop notification instead of picking up the
phone. `NOTIFICATION_ACTION` already exists and carries only `dismiss`, so the
packet type and the key mapping in `plugins/notification.py` are already there.

**The original plan here was wrong.** It assumed a text entry could go on the
freedesktop notification itself. Checked against the live server:
GNOME Shell 50.4 advertises `actions, body, body-markup, icon-static,
persistence, sound` and nothing else — no `inline-reply` — and the interface
carries no `NotificationReplied` signal, only `ActionInvoked`. Inline replies
are a KDE extension to the spec.

**Revised approach.** The notification gets an ordinary *Reply* button. Pressing
it makes the daemon emit `ReplyRequested` over D-Bus; the shell extension opens
a modal dialog with a text entry and calls `SendReply` back. This is the same
division the clipboard already uses and for the same reason — the extension is
the compositor, so it can put UI on screen that a background daemon cannot.
Android's `RemoteInput` does the delivery on the phone side.

- [x] Replying from the desktop delivers the message through the originating
      app — via the notification's own `RemoteInput`, the same mechanism the
      phone's shade uses
- [x] Notifications without a reply action don't grow a dead text box — the
      phone reports `canReply` per notification, including when an app *loses*
      its reply action on an update
- [x] A failed reply says so — the phone sends `reply-failed` and the desktop
      raises an urgent notification. A silently dropped reply is the worst
      outcome: the user believes they answered someone and didn't.

Unverified on hardware. The modal dialog in particular has never been on a
screen — `ModalDialog` and `St.Entry` are stable API across shell 45–50, but
that is reasoning, not evidence.

### Media control — **Built**

**Scoped: control the phone's playback from the desktop**, over RFCOMM. The
direction was decided by evidence rather than preference — Bluetooth's own
AVRCP already does this, *but only while A2DP is connected*, and
`connect_phone_audio` defaults to off precisely so it isn't. Verified on a real
device: dropping A2DP removes BlueZ's `MediaPlayer1` too. So this restores
control the audio fix takes away, instead of duplicating a working profile.

Android exposes active sessions through `MediaSessionManager`, whose access is
gated on the notification-listener grant — which the app already holds for
notification mirroring. No new permission.

- [x] Now playing (title, artist, whether it's playing) reaches the desktop
- [x] Play/pause, next and previous work from Quick Settings
- [x] Nothing playing means no media row, rather than a dead one — the phone
      sends `hasSession: false` explicitly, so the desktop clears rather than
      keeping a stale track
- [x] Controls disappear when the phone disconnects

Also: identical updates don't emit a D-Bus change, so a session reporting in
repeatedly doesn't redraw the menu; and the daemon refuses unknown actions
rather than forwarding something the phone would silently ignore. 13 tests.

Unverified on hardware.

---

## Milestone 4 — Beyond Bluetooth

**This milestone revises a stated design principle.** The README said
FedoraLink deliberately does no file transfer, because RFCOMM's ~100–250 KB/s
makes it hopeless. That number hasn't changed. What changes is the conclusion:
instead of refusing the feature, add a faster transport and use Bluetooth as
the fallback that keeps the "works without a network" promise intact.

Do these in order — file transfer over RFCOMM alone would be a bad first
impression of the feature.

### LAN/TCP transport — **Built**

A second transport used when both devices are on the same network, falling
back to Bluetooth when they aren't.

`RfcommTransport` is a concrete class, not an interface, so the first real
task is extracting a transport abstraction. `Connection` is already separable
enough to reuse — it deals in packets and a file descriptor, not in Bluetooth.

**Decisions taken** (the open questions, answered)

- **Discovery: reuse the Bluetooth link.** The desktop names its address, port
  and a fresh nonce in a `fedoralink.upgrade` packet after authenticating. No
  new dependency, no multicast through a firewall, and nothing to spoof — the
  offer arrives on a channel the peer has already proved itself on. The cost is
  that a LAN session only starts after a Bluetooth handshake, which is the right
  order anyway: that's where the secret and the nonces come from.
- **Encryption: the Milestone 2 secret, not TLS.** HKDF-SHA256 over
  secret + both nonces yields two directional keys; each packet is an
  AES-256-GCM record. Directional because one key both ways would let an
  attacker replay our own records at us; counter-as-nonce because GCM fails
  catastrophically on nonce reuse. TLS was rejected on cost: Android has no
  public X.509 builder, so self-signed certs would mean bundling BouncyCastle.
  Needs `cryptography` on the desktop (a Fedora base package); Android's
  `javax.crypto` has AES-GCM natively.
- **Switching: Bluetooth stays up underneath.** The LAN link is preferred for
  sending while it exists; if it drops the link degrades rather than
  disappearing, and the desktop re-offers. The listening socket exists only
  while a phone is authenticated, so no port is open otherwise.

**Acceptance criteria**

- [~] Same Wi-Fi: connects over TCP — the desktop side is now verified end to
      end against a fake phone in Python: handshake, mutual MAC check, key
      derivation, and a real AES-GCM record decoded. Never run against an
      actual phone, so "measurably faster" is still unmeasured
- [x] No shared network: falls back to Bluetooth, unprompted — the upgrade is
      best-effort and a failure to connect is not an error
- [x] Leaving Wi-Fi mid-session doesn't drop the link — Bluetooth stays
      connected underneath and the desktop re-offers
- [x] An attacker on the same network cannot read the stream — AES-256-GCM
      with keys neither side chooses alone

The crypto is the part that *is* verified: HKDF is pinned to RFC 5869 test
case 1 on both sides, and the Kotlin tests open ciphertext the Python side
actually produced, so the two implementations provably interoperate. The socket
plumbing around it has never carried a byte between two machines.

### File transfer — **Built**

Chunked over the existing NDJSON stream, base64 inside `fedoralink.file.*`
packets. That costs a third more bytes than a binary framing, which is
irrelevant on a LAN and painful over Bluetooth — but it keeps the whole
protocol readable in a terminal, which is the property that makes it
debuggable.

**Done and tested** (desktop, 45 tests in `test_transfers.py`)

- `transfers.py` — the accounting: in-order chunks only, declared size as a
  hard limit, SHA-256 verified before the temp file is renamed into place
- `plugins/files.py` — the I/O, the accept prompt, progress notifications,
  and back-pressure
- `SendFile` / `CancelTransfer` over D-Bus

**Done and compiling** (phone)

- `FileTransfer.kt` — receive into MediaStore Downloads, `IS_PENDING` until
  the hash matches so a corrupt file never reaches the gallery; send from a
  content Uri
- `FileSendActivity.kt` — share sheet for any mime type, single or multiple;
  finishes at once and lets the foreground service carry the transfer
- `LinkService` routes all five `fedoralink.file.*` types

**Still to do**

- [x] A nicer way to start a send from the desktop — `fedoralink send`, plus
      **Scripts → Send to Phone** in Files. A plain Nautilus script rather than
      a `nautilus-python` extension: no extra dependency, and it survives
      Nautilus API changes
- [ ] Run it. Nothing here has moved a byte between a real phone and a real PC

**Acceptance criteria**

- [x] Send from the desktop and from the phone — `SendFile` over D-Bus one
      way, the Android share sheet the other
- [x] Progress is visible, and a transfer can be cancelled — progress
      notification replaces in place rather than stacking one per chunk;
      `CancelTransfer` and `FILE_CANCEL` work both ways
- [x] A dropped link fails cleanly, never a silent half-file — writes go to
      `.name.part` and are renamed only after the hash matches; a disconnect
      mid-transfer discards it
- [x] Over Bluetooth, the UI is honest about how slow it will be — the accept
      prompt says "about 7 minutes over Bluetooth" using RFCOMM's real
      throughput, and omits it on a LAN

Resuming is explicitly *not* implemented: the criterion said "resumes or fails
cleanly", and failing cleanly is the half that's built.

---

## Notes

Running log of decisions and discoveries that changed the plan. Newest first.

- **2026-09-23** — Audit pass. Four real defects, all found by reading rather
  than by anything failing:

  1. **The LAN handshake could freeze the daemon.** It read with a socket
     timeout *inside* the GLib accept callback, so a peer that connected and
     then said nothing stalled everything — notifications, clipboard, Bluetooth
     — for ten seconds. Rewritten as a non-blocking state machine driven by the
     main loop, with a regression test that fails if a timer stops ticking
     during a handshake.
  2. **Lock-on-leave fired on a refused peer.** Any disconnect armed it,
     including a protocol-version refusal or a declined enrollment — so
     updating the daemon and not the app would lock your screen thirty seconds
     later, with the phone in your hand. It now only arms if the session had
     authenticated.
  3. **Media went silent after a reconnect.** The phone skips an unchanged
     payload, but the desktop clears its row on disconnect, so the row stayed
     empty until the track changed.
  4. **The notification title map never shrank.** One entry per mirrored
     notification, cleared only on disconnect.

  Checked and deliberately *not* changed: `audio.py` matches the BlueZ device
  path with `startswith`, which looks like a prefix-collision bug. Device paths
  are fixed-length, so none can prefix another — not a defect, and "fixing" it
  would be noise.

  Two process findings from the same slip. The lint step and the commit went in
  as separate shell commands rather than one chain, so a non-zero exit stopped
  nothing and a lint-failing commit reached main. Worse, **CI did not catch it
  either**: it ran `ruff check daemon/fedoralink`, which skips `daemon/tests`
  entirely, so the bad commit went green. Both steps now cover `daemon/`, tests
  included — a lint gate that cannot see the test suite is not a gate.
- **2026-09-23** — Every planned item is now implemented, which exposed a flaw
  in this file's own legend: eight items sat at **In progress**, defined as
  "someone is actively writing it", when nobody was. There was no state for
  "code complete, never run". Added **Built** and reclassified, because a
  roadmap that overstates its own confidence is exactly what the rule at the
  top exists to prevent.
- **2026-09-23** — Added a CLI, which turned out to be the cheapest way to make
  any of this testable by hand. `fedoralink status` run against the live daemon
  found a real bug in its own output: the old installed daemon has no
  `Authenticated` property, so a plain `get_cached_property(...) or False` read
  it as "authenticated: no" — a lie with the same shape as the truth. It now
  distinguishes an absent property from a false one and says so.
- **2026-09-23** — File transfer complete on both sides and compiling. The
  phone half went via a branch and PR #1 rather than straight to main, because
  CI only builds `main` and pull requests — a branch push compiles nothing, so
  a PR was the only way to get the Kotlin near a compiler before it landed.
  All ten roadmap items are now built. None of the phone-side ones have been
  run on a phone.
- **2026-09-23** — Reported from real use: the phone was routing its audio to
  the PC, so media played out of the laptop. Not something FedoraLink asked
  for — Android auto-connects A2DP to any bonded PC that advertises it, and a
  normal app can't opt out because `setConnectionPolicy` is a system API. The
  desktop can, via `Device1.DisconnectProfile`, so that's where the fix went,
  off by default.

  Verified against the reporter's own Galaxy Z Flip6 rather than reasoned
  about: it advertises `110a`/`110c`/`110e`/`1112`/`111f`, `DisconnectProfile`
  succeeds on `110a` and `111f` and returns "Invalid arguments" for anything
  unadvertised, every `MediaTransport1` disappeared, and `Device1.Connected`
  stayed true — which is the part that matters, since the RFCOMM link rides
  on it. Watching `InterfacesAdded` for `MediaTransport1` beats a timer,
  because Android can connect audio well after our link comes up.

  Correction to the above, found by re-checking after the fact: dropping A2DP
  takes AVRCP with it. The commit claimed media control was preserved because
  AVRCP's UUIDs aren't in the disconnect list — but `MediaPlayer1` vanished
  along with the transport, so the claim was wrong in effect. Fixed in the
  comment and the README. It also settles the media-control scoping below:
  with audio off there is no Bluetooth-native media control, so carrying it
  over RFCOMM restores something real rather than duplicating a profile.
- **2026-09-23** — Notification replies needed a redesign before they could be
  built. The plan assumed a text entry could sit on the freedesktop
  notification; GNOME Shell 50.4 advertises no `inline-reply` and exposes no
  `NotificationReplied` signal, so that is a KDE extension to the spec, not
  something to build on. Checked with `busctl` against the running server
  rather than assumed. The reply box now lives in the shell extension, which
  can show one because it *is* the compositor — the same argument the README
  already makes about clipboard I/O.
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
