/* FedoraLink — GNOME Shell Quick Settings integration.
 *
 * This extension is only a view. All Bluetooth state lives in the
 * fedoralink daemon and arrives here over D-Bus, so GNOME reloading the
 * extension (lock, unlock, monitor change) never drops the phone link.
 */

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';

import {Extension, gettext as _} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const BUS_NAME = 'org.fedoralink.Daemon';
const OBJECT_PATH = '/org/fedoralink/Daemon';

const DaemonInterface = `
<node>
  <interface name="org.fedoralink.Daemon">
    <method name="Ping"/>
    <method name="SendClipboard"/>
    <method name="Reconnect"/>
    <property name="Connected" type="b" access="read"/>
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

        this.menu.setHeader('phone-symbolic', _('FedoraLink'), _('Not connected'));

        this._pingItem = this.menu.addAction(_('Find My Phone'), () => {
            this._call('Ping');
        });
        this._clipboardItem = this.menu.addAction(_('Send Clipboard to Phone'), () => {
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

    _setUnavailable(reason) {
        this.checked = false;
        this.subtitle = reason;
        this.iconName = 'phone-symbolic';

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
});

const FedoraLinkIndicator = GObject.registerClass(
class FedoraLinkIndicator extends SystemIndicator {
    _init() {
        super._init();

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
                this._sync();
            });

        this._propsId = this._proxy.connect('g-properties-changed', () => this._sync());
        // Fires when the daemon starts or stops, so the toggle reflects
        // "not running" without the user having to poke it.
        this._ownerId = this._proxy.connect('notify::g-name-owner', () => this._sync());
    }

    _sync() {
        this._toggle.sync();
        this._indicator.visible =
            this._proxy.g_name_owner !== null && this._proxy.Connected;
    }

    destroy() {
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
