#!/usr/bin/env bash
#
# FedoraLink installer.
#
#   curl -fsSL https://raw.githubusercontent.com/teterw/fedoralink/main/install.sh | bash
#
# Installs entirely into your home directory. The only thing needing root
# is the dnf dependency check, and that is skipped when the packages are
# already present — which they are on a stock Fedora Workstation.

set -euo pipefail

REPO="teterw/fedoralink"
BRANCH="${FEDORALINK_BRANCH:-main}"
EXT_UUID="fedoralink@teterw.github.io"

PREFIX="${HOME}/.local"
APP_DIR="${PREFIX}/share/fedoralink"
BIN_DIR="${PREFIX}/bin"
EXT_DIR="${PREFIX}/share/gnome-shell/extensions/${EXT_UUID}"
UNIT_DIR="${HOME}/.config/systemd/user"

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'

info()  { printf '%s==>%s %s\n' "${BOLD}" "${RESET}" "$*"; }
ok()    { printf '%s  ok%s %s\n' "${GREEN}" "${RESET}" "$*"; }
warn()  { printf '%swarn%s %s\n' "${YELLOW}" "${RESET}" "$*"; }
die()   { printf '%s fail%s %s\n' "${RED}" "${RESET}" "$*" >&2; exit 1; }

# --------------------------------------------------------------- uninstall

uninstall() {
    rm -f "${HOME}/.local/share/nautilus/scripts/Send to Phone"
    info "Removing FedoraLink"
    systemctl --user disable --now fedoralink.service 2>/dev/null || true
    rm -f "${UNIT_DIR}/fedoralink.service"
    systemctl --user daemon-reload 2>/dev/null || true

    gnome-extensions disable "${EXT_UUID}" 2>/dev/null || true
    rm -rf "${EXT_DIR}" "${APP_DIR}" "${BIN_DIR}/fedoralink"

    ok "FedoraLink removed. Log out and back in to clear the panel icon."
    exit 0
}

# Not `[[ ... ]] && uninstall` — under `set -e` a false test would make the
# whole script exit with status 1.
if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
fi

# ------------------------------------------------------------ sanity check

info "Checking your system"

[[ -f /etc/fedora-release ]] || warn "Not Fedora — continuing, but this is only tested on Fedora."

if [[ "${XDG_CURRENT_DESKTOP:-}" != *GNOME* ]]; then
    warn "GNOME not detected. The daemon will work; the Quick Settings toggle will not."
fi

command -v python3 >/dev/null || die "python3 not found."
python3 -c 'import gi' 2>/dev/null || MISSING_GI=1

# Only ask for a password if something is genuinely absent.
NEEDED=()
[[ -n "${MISSING_GI:-}" ]] && NEEDED+=(python3-gobject)
command -v wl-copy   >/dev/null || NEEDED+=(wl-clipboard)
command -v bluetoothctl >/dev/null || NEEDED+=(bluez)
# Ring My PC falls back to a single notification chime without this, which
# is audible but easy to miss when the laptop is under a cushion.
command -v canberra-gtk-play >/dev/null || NEEDED+=(libcanberra-gtk3)
# AES-GCM for the LAN transport. Without it the daemon still runs, and
# still works over Bluetooth — it just won't offer a LAN link.
python3 -c 'import cryptography' 2>/dev/null || NEEDED+=(python3-cryptography)

if (( ${#NEEDED[@]} )); then
    info "Installing missing packages: ${NEEDED[*]}"
    sudo dnf install -y "${NEEDED[@]}" || die "Package install failed."
fi
ok "Dependencies satisfied"

# ------------------------------------------------------------- get sources

WORKDIR=""
cleanup() { [[ -n "${WORKDIR}" ]] && rm -rf "${WORKDIR}"; }
trap cleanup EXIT

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"

if [[ -n "${SCRIPT_DIR}" && -d "${SCRIPT_DIR}/daemon/fedoralink" ]]; then
    SRC="${SCRIPT_DIR}"
    info "Installing from local checkout"
else
    # Piped from curl: no checkout on disk, so fetch a tarball.
    info "Downloading FedoraLink (${BRANCH})"
    WORKDIR="$(mktemp -d)"
    curl -fsSL "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" \
        | tar xz -C "${WORKDIR}" --strip-components=1 \
        || die "Download failed. Is the repository public and the branch correct?"
    SRC="${WORKDIR}"
fi

[[ -d "${SRC}/daemon/fedoralink" ]] || die "Source tree looks wrong (no daemon/fedoralink)."

# ----------------------------------------------------------------- install

info "Installing daemon to ${APP_DIR}"
rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}" "${BIN_DIR}" "${UNIT_DIR}"
cp -r "${SRC}/daemon/fedoralink" "${APP_DIR}/"

# PYTHONPATH is baked in so the package resolves no matter where the
# launcher is invoked from — systemd starts it with an unrelated cwd.
cat > "${BIN_DIR}/fedoralink" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${APP_DIR}:\${PYTHONPATH:-}"
exec python3 -m fedoralink "\$@"
EOF
chmod +x "${BIN_DIR}/fedoralink"
ok "Daemon installed"

# Right-click → Scripts → Send to Phone. A plain script, so it needs no
# nautilus-python and survives Nautilus API changes.
NAUTILUS_SCRIPTS="${HOME}/.local/share/nautilus/scripts"
if [[ -f "${SRC}/daemon/data/nautilus-send-to-phone" ]]; then
    mkdir -p "${NAUTILUS_SCRIPTS}"
    install -m 755 "${SRC}/daemon/data/nautilus-send-to-phone" \
        "${NAUTILUS_SCRIPTS}/Send to Phone"
    ok "Files integration installed"
fi

info "Installing GNOME Shell extension"
rm -rf "${EXT_DIR}"
mkdir -p "${EXT_DIR}"
cp "${SRC}/shell-extension/metadata.json" "${SRC}/shell-extension/extension.js" "${EXT_DIR}/"
ok "Extension installed"

info "Enabling background service"
cp "${SRC}/daemon/data/fedoralink.service" "${UNIT_DIR}/fedoralink.service"
systemctl --user daemon-reload
systemctl --user enable fedoralink.service
# restart, not `enable --now`: on an upgrade the service is already
# active, and `--now` would leave the previous version running.
systemctl --user restart fedoralink.service
ok "Service running"

# --------------------------------------------------------------- finish up

if ! grep -q "${BIN_DIR}" <<< "${PATH}"; then
    warn "${BIN_DIR} is not on your PATH — add it to run 'fedoralink' by hand."
fi

echo
printf '%sFedoraLink installed.%s\n\n' "${BOLD}${GREEN}" "${RESET}"

cat <<'NEXT'
Next steps:

  1. Log out and back in.
     GNOME Shell cannot load a new extension on Wayland without a fresh
     session. This step is not optional.

  2. Enable the extension:
       gnome-extensions enable fedoralink@teterw.github.io

  3. Install the Android app from:
       https://github.com/teterw/fedoralink/releases

  4. Pair your phone with this PC in GNOME Settings > Bluetooth.

  5. Open FedoraLink on the phone, pick this PC, grant the permissions,
     and flip "Enable link".

The toggle appears in the Quick Settings panel (top right) once a phone
is paired and the app is running.

Check on the daemon any time with:
    systemctl --user status fedoralink
    journalctl --user -u fedoralink -f

Uninstall with:
    curl -fsSL https://raw.githubusercontent.com/teterw/fedoralink/main/install.sh | bash -s -- --uninstall
NEXT
