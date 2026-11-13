#!/usr/bin/env bash
set -euo pipefail

PKG_NAME="pve-storage-gdrive"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="/var/backups/pve-storage-gdrive"
TPL_FILE="/usr/share/pve-manager/index.html.tpl"

ASSUME_YES=0
PURGE_CONFIG=0

log_info() {
    printf '[INFO] %s\n' "$*"
}

log_warn() {
    printf '[WARN] %s\n' "$*" >&2
}

log_err() {
    printf '[ERROR] %s\n' "$*" >&2
}

die() {
    log_err "$*"
    exit 1
}

usage() {
    cat <<EOF
$PKG_NAME revert script (restores Proxmox VE files to pre-install defaults)

Usage:
  ./revert.sh [OPTIONS]

Options:
  --purge-config    Also remove /etc/pve/priv/gdrive credentials
  --yes, -y         Skip interactive confirmation prompts
  --help, -h        Show this help message and exit
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --purge-config)
                PURGE_CONFIG=1
                shift
                ;;
            --yes|-y)
                ASSUME_YES=1
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                die "Unknown option: $1 (run with --help for usage)"
                ;;
        esac
    done
}

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        die "This script must be run as root (try sudo ./revert.sh)."
    fi
}

confirm_action() {
    local prompt="$1"
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        die "Non-interactive shell detected. Re-run with --yes to proceed without prompting."
    fi
    local reply
    read -r -p "$prompt [y/N] " reply
    case "$reply" in
        [yY]|[yY][eE][sS])
            return 0
            ;;
        *)
            log_info "Cancelled by user."
            exit 0
            ;;
    esac
}

main() {
    parse_args "$@"
    require_root

    confirm_action "Revert $PKG_NAME and restore Proxmox VE files to default on $(hostname)?"

    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
        systemctl stop pve-gdrived.service 2>/dev/null || true
        systemctl disable pve-gdrived.service 2>/dev/null || true
    fi

    # Remove overlay markers first while hook may still be present
    if [[ -x /usr/libexec/pve-gdrive/overlay-hook.sh ]]; then
        /usr/libexec/pve-gdrive/overlay-hook.sh --remove || true
    elif [[ -x "$SCRIPT_DIR/ui/overlay-hook.sh" ]]; then
        "$SCRIPT_DIR/ui/overlay-hook.sh" --remove || true
    fi

    if dpkg-query -W -f='${Status}' "$PKG_NAME" 2>/dev/null | grep -q "install ok installed\|half-configured\|unpacked"; then
        log_info "Removing $PKG_NAME package..."
        DEBIAN_FRONTEND=noninteractive apt-get remove -y "$PKG_NAME" || dpkg --remove --force-remove-reinstreq "$PKG_NAME" || true
    fi

    # Restore pristine original index.html.tpl if backed up
    if [[ -f "$BACKUP_ROOT/index.html.tpl.orig" && -f "$TPL_FILE" ]]; then
        cat "$BACKUP_ROOT/index.html.tpl.orig" > "$TPL_FILE"
        log_info "Restored $TPL_FILE from $BACKUP_ROOT/index.html.tpl.orig"
    elif [[ -x "$SCRIPT_DIR/ui/overlay-hook.sh" && -f "$TPL_FILE" ]]; then
        "$SCRIPT_DIR/ui/overlay-hook.sh" --remove || true
        log_info "Removed pve-storage-gdrive overlay markers from $TPL_FILE"
    fi

    # Clean up any leftover plugin/service files
    rm -f \
        /usr/share/perl5/PVE/Storage/Custom/GDrivePlugin.pm \
        /usr/share/perl5/PVE/API2/GDrive.pm \
        /usr/share/pve-manager/js/pve-gdrive.js \
        /usr/bin/pve-gdrive \
        /lib/systemd/system/pve-gdrived.service
    rm -rf /usr/libexec/pve-gdrive

    if [[ "$PURGE_CONFIG" -eq 1 && -d /etc/pve/priv/gdrive ]]; then
        rm -rf /etc/pve/priv/gdrive
        log_info "Removed /etc/pve/priv/gdrive (--purge-config)"
    fi

    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
        systemctl daemon-reload
        for svc in pvedaemon pveproxy pvestatd; do
            if systemctl is-enabled --quiet "$svc" 2>/dev/null || systemctl is-active --quiet "$svc" 2>/dev/null; then
                log_info "Restarting $svc..."
                systemctl restart "$svc"
            fi
        done
    fi

    log_info "Revert complete. Proxmox VE has been restored to default."
}

main "$@"
