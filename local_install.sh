#!/usr/bin/env bash
set -euo pipefail

PKG_NAME="pve-storage-gdrive"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"

ASSUME_YES=0
UNINSTALL=0
SKIP_BUILD=0

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
$PKG_NAME local installer (installs directly from a cloned git repository)

Usage:
  ./local_install.sh [OPTIONS]

Options:
  --skip-build      Skip rebuilding the .deb and install the latest .deb in dist/
  --uninstall       Remove $PKG_NAME from this Proxmox node
  --yes, -y         Skip interactive confirmation prompts
  --help, -h        Show this help message and exit
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --skip-build)
                SKIP_BUILD=1
                shift
                ;;
            --uninstall)
                UNINSTALL=1
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
        die "This installer must be run as root (try sudo ./local_install.sh)."
    fi
}

require_tools() {
    local tools=("apt-get" "systemctl" "dpkg-query")
    if [[ "$UNINSTALL" -eq 0 ]]; then
        tools+=("sha256sum" "awk")
        if [[ "$SKIP_BUILD" -eq 0 ]]; then
            tools+=("python3")
        fi
    fi
    local tool
    for tool in "${tools[@]}"; do
        command -v "$tool" >/dev/null 2>&1 || die "Required command not found: $tool"
    done
}

check_proxmox_host() {
    command -v pveversion >/dev/null 2>&1 || die "pveversion not found; this script supports Proxmox VE 8.x and 9.x only."
    [[ -d /etc/pve ]] || die "/etc/pve does not exist; pmxcfs does not appear to be mounted on this host."

    local pve_ver_out major
    pve_ver_out="$(pveversion 2>/dev/null || true)"
    if [[ "$pve_ver_out" =~ pve-manager/([0-9]+)\. ]]; then
        major="${BASH_REMATCH[1]}"
    else
        die "Could not determine Proxmox VE version from pveversion output: $pve_ver_out"
    fi

    if [[ "$major" != "8" && "$major" != "9" ]]; then
        die "Unsupported Proxmox VE major version ($major). Supported versions: 8.x and 9.x."
    fi

    log_info "Detected Proxmox VE $major.x ($pve_ver_out)"
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

warn_if_cluster() {
    if [[ -f /etc/pve/corosync.conf ]]; then
        echo
        log_warn "Proxmox cluster detected (/etc/pve/corosync.conf exists)."
        log_warn "Storage and OAuth/relay config in /etc/pve replicate across nodes,"
        log_warn "but you MUST run ./local_install.sh on EVERY cluster node so pve-gdrived"
        log_warn "and the PVE storage/UI plugins are installed locally on each peer."
    fi
}

do_uninstall() {
    check_proxmox_host
    confirm_action "Uninstall $PKG_NAME from $(hostname)?"

    if dpkg-query -W -f='${Status}' "$PKG_NAME" 2>/dev/null | grep -q "install ok installed"; then
        log_info "Removing $PKG_NAME..."
        DEBIAN_FRONTEND=noninteractive apt-get remove -y "$PKG_NAME"
        log_info "$PKG_NAME has been removed."
    else
        log_warn "$PKG_NAME is not currently installed."
    fi

    log_info "Note: Credentials under /etc/pve/priv/gdrive and remote Google Drive files were preserved."
    warn_if_cluster
}

BACKUP_ROOT="/var/backups/pve-storage-gdrive"

backup_proxmox_files() {
    local ts snap_dir
    ts="$(date +%Y%m%d-%H%M%S)"
    snap_dir="$BACKUP_ROOT/$ts"
    install -d -m 0700 "$BACKUP_ROOT" "$snap_dir"

    local tpl="/usr/share/pve-manager/index.html.tpl"
    if [[ -f "$tpl" ]]; then
        if ! grep -Fq "# BEGIN pve-storage-gdrive" "$tpl" && [[ ! -f "$BACKUP_ROOT/index.html.tpl.orig" ]]; then
            cp -p "$tpl" "$BACKUP_ROOT/index.html.tpl.orig"
            log_info "Saved pristine original template to $BACKUP_ROOT/index.html.tpl.orig"
        fi
        cp -p "$tpl" "$snap_dir/index.html.tpl"
    fi

    local api2_pm="/usr/share/perl5/PVE/API2.pm"
    if [[ -f "$api2_pm" ]]; then
        if ! grep -Fq "# BEGIN pve-storage-gdrive" "$api2_pm" && [[ ! -f "$BACKUP_ROOT/API2.pm.orig" ]]; then
            cp -p "$api2_pm" "$BACKUP_ROOT/API2.pm.orig"
            log_info "Saved pristine original API2.pm to $BACKUP_ROOT/API2.pm.orig"
        fi
        cp -p "$api2_pm" "$snap_dir/API2.pm"
    fi

    if [[ -f /etc/vzdump.conf ]]; then
        cp -p /etc/vzdump.conf "$snap_dir/vzdump.conf"
    fi
    if [[ -f /etc/pve/storage.cfg ]]; then
        cp -p /etc/pve/storage.cfg "$snap_dir/storage.cfg"
    fi
    log_info "Pre-install backup saved to $snap_dir"
}

do_install() {
    check_proxmox_host

    [[ -f "$REPO_DIR/scripts/build-deb.py" ]] || die "Repository files not found in $REPO_DIR (expected scripts/build-deb.py)."

    echo "================================================================"
    echo " $PKG_NAME local installer (from git clone: $REPO_DIR)"
    echo "================================================================"

    confirm_action "Build and install $PKG_NAME on $(hostname)?"

    local dist_dir="$REPO_DIR/dist"
    if [[ "$SKIP_BUILD" -eq 0 ]]; then
        log_info "Building .deb package from $REPO_DIR..."
        python3 "$REPO_DIR/scripts/build-deb.py"
    else
        log_info "Skipping .deb build (--skip-build); looking in $dist_dir"
    fi

    [[ -f "$dist_dir/SHA256SUMS" ]] || die "Missing $dist_dir/SHA256SUMS."
    local deb_name deb_path
    deb_name="$(awk '/pve-storage-gdrive_.*_all\.deb$/ {f=$2; sub(/^\*/, "", f); sub(/^.*\//, "", f); print f; exit}' "$dist_dir/SHA256SUMS")"
    [[ -n "$deb_name" ]] || die "Could not locate pve-storage-gdrive .deb entry in $dist_dir/SHA256SUMS."

    deb_path="$dist_dir/$deb_name"
    [[ -f "$deb_path" ]] || die "Built package not found at $deb_path."

    log_info "Verifying SHA-256 checksum for $deb_name..."
    (cd "$dist_dir" && sha256sum -c SHA256SUMS)

    backup_proxmox_files

    log_info "Updating APT package lists and installing $deb_path..."
    DEBIAN_FRONTEND=noninteractive apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall "$deb_path"

    if ! systemctl is-active --quiet pve-gdrived.service; then
        systemctl status pve-gdrived.service --no-pager || true
        die "pve-gdrived.service is not active after installation."
    fi

    log_info "pve-gdrived.service is active and running."
    warn_if_cluster

    if [[ -t 0 && "$ASSUME_YES" -eq 0 ]]; then
        echo
        local login_reply
        read -r -p "Sign in to your Google Drive account now in the CLI? [Y/n] " login_reply
        case "$login_reply" in
            [nN]|[nN][oO])
                log_info "Skipped CLI login. You can run 'pve-gdrive login' anytime or sign in from the Web UI."
                ;;
            *)
                pve-gdrive login || log_warn "CLI login did not complete. You can re-run 'pve-gdrive login' anytime."
                ;;
        esac
    fi

    echo
    log_info "Local installation complete!"
    log_info "Next steps:"
    echo "  1. Sign in from CLI anytime with: pve-gdrive login"
    echo "  2. Open Proxmox Web UI -> Datacenter -> Storage -> Add -> Google Drive."
    echo "  3. Select your Google account (or click 'Sign in with Google')."
    echo "  4. CLI status check: pve-gdrive status"
}

main() {
    parse_args "$@"
    require_root
    require_tools

    if [[ "$UNINSTALL" -eq 1 ]]; then
        do_uninstall
    else
        do_install
    fi
}

main "$@"
