/* FedoraLink — GNOME Shell Quick Settings integration.
 *
 * This extension is only a view. All Bluetooth state lives in the
 * fedoralink daemon and arrives here over D-Bus, so GNOME reloading the
 * extension (lock, unlock, monitor change) never drops the phone link.
 */

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';

import Clutter from 'gi://Clutter';

import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';

const BUS_NAME = 'org.fedoralink.Daemon';
const OBJECT_PATH = '/org/fedoralink/Daemon';

// Must match RING_DURATION_MS in Ringer.kt. The phone silences itself when
// its own timer expires, so this is when "Stop Ringing" stops being useful.
const RING_DURATION_MS = 15000;

const DaemonInterface = `
<node>
  <interface name="org.fedoralink.Daemon">
    <method name="Ping"/>
    <method name="StopRinging"/>
    <method name="SendClipboard"/>
    <method name="Reconnect"/>
    <method name="ForgetDevices">
      <arg name="count" type="i" direction="out"/>
    </method>
    <method name="SetClipboard">
      <arg name="content" type="s" direction="in"/>
      <arg name="force" type="b" direction="in"/>
    </method>
    <method name="SetClipboardBridge">
      <arg name="active" type="b" direction="in"/>
    </method>
    <method name="SendReply">
      <arg name="key" type="s" direction="in"/>
      <arg name="text" type="s" direction="in"/>
    </method>
    <signal name="ClipboardChanged">
      <arg name="content" type="s"/>
    </signal>
    <signal name="ReplyRequested">
      <arg name="key" type="s"/>
      <arg name="title" type="s"/>
    </signal>
    <property name="Connected" type="b" access="read"/>
    <property name="Authenticated" type="b" access="read"/>
    <property name="DeviceName" type="s" access="read"/>
    <property name="BatteryLevel" type="i" access="read"/>
    <property name="BatteryCharging" type="b" access="read"/>
  </interface>
</node>`;

const DaemonProxy = Gio.DBusProxy.makeProxyWrapper(DaemonInterface);

/* Pick the icon GNOME already ships for this battery level, so the
 * toggle matches the system's own battery indicator. */
function batteryIconName(level, charging) {
    if (level < 0)
        return 'battery-missing-symbolic';

    const step = Math.max(0, Math.min(100, Math.round(level / 10) * 10));
    return charging
        ? `battery-level-${step}-charging-symbolic`
        : `battery-level-${step}-symbolic`;
}

/* The reply box GNOME's notification server can't provide.
 *
 * org.freedesktop.Notifications on GNOME Shell advertises actions, body,
 * body-markup, icon-static, persistence and sound — no inline-reply, and
 * no NotificationReplied signal. Inline replies are a KDE extension to the
 * spec, so a Reply button on a notification can only start a conversation,
 * not finish one.
 *
 * The extension can finish it, for the same reason it does clipboard I/O:
 * it runs inside the compositor, so it can put a focused text entry on
 * screen without being an application with a window.
 */
const ReplyDialog = GObject.registerClass(
class ReplyDialog extends ModalDialog.ModalDialog {
    _init(title, onSubmit) {
        super._init({styleClass: 'run-dialog'});

        this._onSubmit = onSubmit;

        this.contentLayout.add_child(new St.Label({
            text: title ? _('Reply to %s').format(title) : _('Reply'),
            style_class: 'run-dialog-label',
        }));

        this._entry = new St.Entry({
            can_focus: true,
            hint_text: _('Type your reply…'),
            style_class: 'run-dialog-entry',
        });
        this._entry.clutter_text.set_activatable(true);
        this._entry.clutter_text.connect('activate', () => this._submit());
        this.contentLayout.add_child(this._entry);

        this.setButtons([
            {
                label: _('Cancel'),
                action: () => this.close(),
                key: Clutter.KEY_Escape,
            },
            {
                label: _('Send'),
                action: () => this._submit(),
                default: true,
            },
        ]);

        this.setInitialKeyFocus(this._entry.clutter_text);
    }

    _submit() {
        const text = this._entry.get_text().trim();
        this.close();
        // An empty reply is a cancel, not a message worth sending to
        // whoever is waiting on the other end.
        if (text)
            this._onSubmit(text);
    }
});

/* Clipboard I/O on the daemon's behalf.
 *
 * The daemon can't read the Wayland selection without spawning wl-paste,
 * and without the data-control protocol (which Mutter still lacks) every
 * such spawn maps a real toplevel window — which is why polling used to
 * flash a wl-clipboard icon in and out of the dock every two seconds.
 *
 * The shell has no such problem: it *is* the compositor. It reads and
 * writes the selection directly, and Mutter gives it an owner-changed
 * signal, so sync is event-driven rather than polled.
 */
class ClipboardBridge {
    constructor(proxy) {
        this._proxy = proxy;
        this._clipboard = St.Clipboard.get_default();

        // Last value we exchanged with the daemon, so setting the
        // clipboard from the phone doesn't bounce straight back.
        this._lastValue = null;

        this._selection = global.display.get_selection();
        this._ownerChangedId = this._selection.connect(
            'owner-changed', (_selection, type) => {
                if (type === Meta.SelectionType.SELECTION_CLIPBOARD)
                    this._onLocalCopy();
            });

        this._signalId = this._proxy.connectSignal(
            'ClipboardChanged', (_p, _s, [content]) => this._onRemote(content));

        this._proxy.SetClipboardBridgeRemote(true, () => {});
    }

    _onLocalCopy() {
        this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => {
            if (!text || text === this._lastValue)
                return;

            this._lastValue = text;
            this._proxy.SetClipboardRemote(text, false, () => {});
        });
    }

    _onRemote(content) {
        if (!content || content === this._lastValue)
            return;

        this._lastValue = content;
        this._clipboard.set_text(St.ClipboardType.CLIPBOARD, content);
    }

    /* Quick Settings "Send Clipboard to Phone": resend even when the
     * content hasn't changed, since the user asked for it explicitly. */
    sendCurrent() {
        this._clipboard.get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => {
            if (!text)
                return;

            this._lastValue = text;
            this._proxy.SetClipboardRemote(text, true, () => {});
        });
    }

    destroy() {
        if (this._ownerChangedId) {
            this._selection.disconnect(this._ownerChangedId);
            this._ownerChangedId = null;
        }
        if (this._signalId) {
            this._proxy.disconnectSignal(this._signalId);
            this._signalId = null;
        }
        // Best effort: if the shell is going down the daemon's name watch
        // catches it anyway.
        this._proxy.SetClipboardBridgeRemote(false, () => {});
        this._proxy = null;
        this._selection = null;
    }
}

const FedoraLinkToggle = GObject.registerClass(
class FedoraLinkToggle extends QuickMenuToggle {
    _init() {
        super._init({
            title: _('FedoraLink'),
            iconName: 'phone-symbolic',
            // Nothing to toggle: tapping opens the menu instead of
            // flipping a switch, matching how Wi-Fi's arrow behaves.
            toggleMode: false,
        });

        this._proxy = null;
        this._ringTimeoutId = 0;

        this.menu.setHeader('phone-symbolic', _('FedoraLink'), _('Not connected'));

        this._pingItem = this.menu.addAction(_('Find My Phone'), () => {
            this._call('Ping');
            this._showStopRinging();
        });
        // Nothing to stop until you've started a ring, so this stays out
        // of the menu rather than sitting there inert.
        this._stopRingItem = this.menu.addAction(_('Stop Ringing'), () => {
            this._call('StopRinging');
            this._hideStopRinging();
        });
        this._stopRingItem.visible = false;
        // Set by the indicator once the bridge exists; falls back to the
        // daemon's own resend if it doesn't.
        this.onSendClipboard = null;
        this._clipboardItem = this.menu.addAction(_('Send Clipboard to Phone'), () => {
            if (this.onSendClipboard)
                this.onSendClipboard();
            else
                this._call('SendClipboard');
        });
        this._reconnectItem = this.menu.addAction(_('Reconnect'), () => {
            this._call('Reconnect');
        });

        this.connect('clicked', () => this.menu.open());

        this._setUnavailable(_('Daemon not running'));
    }

    setProxy(proxy) {
        this._proxy = proxy;
        this.sync();
    }

    _call(method) {
        if (!this._proxy)
            return;
        // Fire and forget — every one of these is advisory, and a failure
        // just means the phone stepped out of range.
        this._proxy[`${method}Remote`](() => {});
    }

    _showStopRinging() {
        this._stopRingItem.visible = true;

        if (this._ringTimeoutId)
            GLib.Source.remove(this._ringTimeoutId);

        // Drop the item when the phone's own timer runs out, so it never
        // offers to stop a ring that already stopped itself.
        this._ringTimeoutId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, RING_DURATION_MS, () => {
                this._ringTimeoutId = 0;
                this._stopRingItem.visible = false;
                return GLib.SOURCE_REMOVE;
            });
    }

    _hideStopRinging() {
        if (this._ringTimeoutId) {
            GLib.Source.remove(this._ringTimeoutId);
            this._ringTimeoutId = 0;
        }
        this._stopRingItem.visible = false;
    }

    _setUnavailable(reason) {
        this.checked = false;
        this.subtitle = reason;
        this.iconName = 'phone-symbolic';

        // Whatever was ringing, we can no longer stop it from here.
        this._hideStopRinging();

        for (const item of [this._pingItem, this._clipboardItem])
            item.reactive = false;
        // Reconnect stays live: it's the one action worth trying while
        // disconnected.
        this._reconnectItem.reactive = !!this._proxy;

        this.menu.setHeader('phone-symbolic', _('FedoraLink'), reason);
    }

    sync() {
        if (!this._proxy || this._proxy.g_name_owner === null) {
            this._setUnavailable(_('Daemon not running'));
            return;
        }

        if (!this._proxy.Connected) {
            this._setUnavailable(_('No phone connected'));
            return;
        }

        if (!this._proxy.Authenticated) {
            // Connected, but the phone hasn't proved who it is yet — and
            // until it does the daemon refuses to send anything, so the
            // actions would silently do nothing.
            this._setUnavailable(_('Verifying phone…'));
            return;
        }

        const name = this._proxy.DeviceName || _('Phone');
        const level = this._proxy.BatteryLevel;
        const charging = this._proxy.BatteryCharging;

        this.checked = true;
        this.iconName = 'phone-symbolic';

        let status;
        if (level < 0)
            status = _('Connected');
        else if (charging)
            status = `${level}% · ${_('Charging')}`;
        else
            status = `${level}%`;

        this.subtitle = status;
        this.menu.setHeader(batteryIconName(level, charging), name, status);

        for (const item of [this._pingItem, this._clipboardItem, this._reconnectItem])
            item.reactive = true;
    }

    destroy() {
        // A live timeout would fire into a destroyed toggle on disable.
        this._hideStopRinging();
        super.destroy();
    }
});

const FedoraLinkIndicator = GObject.registerClass(
class FedoraLinkIndicator extends SystemIndicator {
    _init() {
        super._init();

        this._bridge = null;
        this._replyId = null;
        this._indicator = this._addIndicator();
        this._indicator.iconName = 'phone-symbolic';
        this._indicator.visible = false;

        this._toggle = new FedoraLinkToggle();
        this.quickSettingsItems.push(this._toggle);

        this._proxy = new DaemonProxy(
            Gio.DBus.session, BUS_NAME, OBJECT_PATH,
            (proxy, error) => {
                if (error) {
                    console.error(`FedoraLink: could not reach daemon: ${error.message}`);
                    return;
                }
                this._toggle.setProxy(proxy);
                this._bridge = new ClipboardBridge(proxy);
                this._toggle.onSendClipboard = () => this._bridge.sendCurrent();
                this._replyId = proxy.connectSignal(
                    'ReplyRequested', (_p, _s, [key, title]) =>
                        this._askForReply(key, title));
                this._sync();
            });

        this._propsId = this._proxy.connect('g-properties-changed', () => this._sync());
        // Fires when the daemon starts or stops, so the toggle reflects
        // "not running" without the user having to poke it.
        this._ownerId = this._proxy.connect('notify::g-name-owner', () => this._sync());
    }

    _askForReply(key, title) {
        const dialog = new ReplyDialog(title, text => {
            this._proxy.SendReplyRemote(key, text, () => {});
        });
        dialog.open();
    }

    _sync() {
        this._toggle.sync();
        this._indicator.visible =
            this._proxy.g_name_owner !== null &&
            this._proxy.Connected &&
            this._proxy.Authenticated;
    }

    destroy() {
        this._bridge?.destroy();
        this._bridge = null;

        if (this._replyId) {
            this._proxy.disconnectSignal(this._replyId);
            this._replyId = null;
        }

        if (this._propsId) {
            this._proxy.disconnect(this._propsId);
            this._propsId = null;
        }
        if (this._ownerId) {
            this._proxy.disconnect(this._ownerId);
            this._ownerId = null;
        }
        this._proxy = null;

        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class FedoraLinkExtension extends Extension {
    enable() {
        this._indicator = new FedoraLinkIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
