#!/usr/bin/env bash
set -euo pipefail

PKG_NAME="pve-storage-gdrive"
ONE_LINER='bash -c "$(curl -fsSL https://install.lipa.run/proxmox-gdrive.sh)"'
RELEASE_BASE="${PVE_GDRIVE_RELEASE_BASE:-https://github.com/LipekX/proxmox-gdrive/releases}"
SHA256SUMS_URL_OVERRIDE="${PVE_GDRIVE_SHA256SUMS_URL:-}"

VERSION=""
DEB_URL=""
UNINSTALL=0
ASSUME_YES=0
TMP_DIR=""

cleanup() {
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf -- "$TMP_DIR"
    fi
}
trap cleanup EXIT INT TERM

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
pve-storage-gdrive installer (Proxmox VE 8.x & 9.x)

One-line install:
  $ONE_LINER

Usage:
  install.sh [OPTIONS]

Options:
  --version <ver>   Install a specific release version (e.g. 0.1.0-1)
  --deb-url <url>   Download the .deb package from a custom URL
  --uninstall       Remove pve-storage-gdrive from this Proxmox node
  --yes, -y         Skip interactive confirmation prompts
  --help, -h        Show this help message and exit

Environment overrides:
  PVE_GDRIVE_RELEASE_BASE     Base releases URL (default: $RELEASE_BASE)
  PVE_GDRIVE_SHA256SUMS_URL   Explicit SHA256SUMS URL for --deb-url
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version)
                [[ $# -ge 2 ]] || die "--version requires a value"
                VERSION="$2"
                shift 2
                ;;
            --version=*)
                VERSION="${1#*=}"
                [[ -n "$VERSION" ]] || die "--version requires a non-empty value"
                shift
                ;;
            --deb-url)
                [[ $# -ge 2 ]] || die "--deb-url requires a value"
                DEB_URL="$2"
                shift 2
                ;;
            --deb-url=*)
                DEB_URL="${1#*=}"
                [[ -n "$DEB_URL" ]] || die "--deb-url requires a non-empty value"
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

    if [[ -n "$VERSION" && -n "$DEB_URL" ]]; then
        die "--version and --deb-url cannot be used together"
    fi
}

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        die "This installer must be run as root (try sudo or a root shell)."
    fi
}

require_tools() {
    local tools=("apt-get" "systemctl")
    if [[ "$UNINSTALL" -eq 0 ]]; then
        tools+=("curl" "sha256sum" "awk")
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
        log_warn "Storage and Google account credentials in /etc/pve replicate across nodes,"
        log_warn "but you MUST run the installer on EVERY cluster node so pve-gdrived and the"
        log_warn "PVE storage/UI plugins are installed locally on each peer:"
        echo "  $ONE_LINER"
    fi
}

do_uninstall() {
    check_proxmox_host
    confirm_action "Uninstall $PKG_NAME from $(hostname)?"

    if dpkg-query -W -f='${Status}' "$PKG_NAME" 2>/dev/null | grep -qE "install ok installed|half-configured|unpacked"; then
        log_info "Removing $PKG_NAME..."
        DEBIAN_FRONTEND=noninteractive apt-get remove -y "$PKG_NAME" || dpkg --remove --force-remove-reinstreq "$PKG_NAME"
        log_info "$PKG_NAME has been removed."
    else
        log_warn "$PKG_NAME is not currently installed."
    fi

    log_info "Note: Credentials under /etc/pve/priv/gdrive and remote Google Drive files were preserved."
    warn_if_cluster
}

verify_checksum() {
    local sums_file="$1"
    local deb_path="$2"
    local deb_name expected actual

    deb_name="$(basename "$deb_path")"
    expected="$(awk -v name="$deb_name" '
        /^[0-9a-fA-F]{64}[[:space:]]+/ {
            file=$2
            sub(/^\*/, "", file)
            sub(/^.*\//, "", file)
            if (file == name) {
                print tolower($1)
                exit
            }
        }
    ' "$sums_file")"

    [[ -n "$expected" ]] || die "No SHA-256 entry found for '$deb_name' in SHA256SUMS."

    actual="$(sha256sum "$deb_path" | awk '{print tolower($1)}')"
    if [[ "$actual" != "$expected" ]]; then
        die "SHA-256 checksum mismatch for $deb_name (expected $expected, got $actual)."
    fi

    log_info "Verified SHA-256 checksum for $deb_name ($actual)"
}

do_install() {
    check_proxmox_host

    echo "================================================================"
    echo " pve-storage-gdrive installer for Proxmox VE 8.x & 9.x"
    echo " Source: https://github.com/LipekX/proxmox-gdrive"
    echo " One-liner: $ONE_LINER"
    echo "----------------------------------------------------------------"
    echo " Disclaimer: Unofficial community plugin (AGPL-3.0-or-later)."
    echo " Not affiliated with Proxmox Server Solutions GmbH or Google LLC."
    echo " Provided AS-IS without warranty. Always test backup restores."
    echo "================================================================"

    confirm_action "Install $PKG_NAME on $(hostname)?"

    TMP_DIR="$(mktemp -d -t pve-gdrive-install.XXXXXX)"
    local sums_url deb_url deb_name deb_path sums_path
    sums_path="$TMP_DIR/SHA256SUMS"

    if [[ -n "$DEB_URL" ]]; then
        deb_url="$DEB_URL"
        deb_name="$(basename "${deb_url%%\?*}")"
        if [[ -n "$SHA256SUMS_URL_OVERRIDE" ]]; then
            sums_url="$SHA256SUMS_URL_OVERRIDE"
        else
            sums_url="${deb_url%/*}/SHA256SUMS"
        fi
        log_info "Downloading SHA256SUMS from $sums_url"
        curl -fsSL "$sums_url" -o "$sums_path" || die "Failed to download SHA256SUMS from $sums_url"
    else
        if [[ -n "$VERSION" ]]; then
            local clean_ver="${VERSION#v}"
            local tag="v${clean_ver}"
            sums_url="${RELEASE_BASE}/download/${tag}/SHA256SUMS"
            log_info "Downloading SHA256SUMS for release $tag from $sums_url"
            curl -fsSL "$sums_url" -o "$sums_path" || die "Failed to download SHA256SUMS from $sums_url"
            deb_name="$(awk -v ver="$clean_ver" '
                /pve-storage-gdrive_.*_all\.deb$/ {
                    file=$2
                    sub(/^\*/, "", file)
                    sub(/^.*\//, "", file)
                    if (index(file, "_" ver "_") > 0 || index(file, "_" ver "-") > 0) {
                        print file
                        exit
                    }
                }
            ' "$sums_path")"
            if [[ -z "$deb_name" ]]; then
                deb_name="$(awk '/pve-storage-gdrive_.*_all\.deb$/ {f=$2; sub(/^\*/, "", f); sub(/^.*\//, "", f); print f; exit}' "$sums_path")"
            fi
            [[ -n "$deb_name" ]] || die "Could not locate .deb filename in $sums_url"
            deb_url="${RELEASE_BASE}/download/${tag}/${deb_name}"
        else
            sums_url="${RELEASE_BASE}/latest/download/SHA256SUMS"
            log_info "Downloading latest SHA256SUMS from $sums_url"
            curl -fsSL "$sums_url" -o "$sums_path" || die "Failed to download SHA256SUMS from $sums_url"
            deb_name="$(awk '/pve-storage-gdrive_.*_all\.deb$/ {f=$2; sub(/^\*/, "", f); sub(/^.*\//, "", f); print f; exit}' "$sums_path")"
            [[ -n "$deb_name" ]] || die "Could not locate pve-storage-gdrive .deb filename in $sums_url"
            deb_url="${RELEASE_BASE}/latest/download/${deb_name}"
        fi
    fi

    deb_path="$TMP_DIR/$deb_name"
    log_info "Downloading $deb_url"
    curl -fsSL "$deb_url" -o "$deb_path" || die "Failed to download $deb_url"

    verify_checksum "$sums_path" "$deb_path"

    log_info "Updating APT package lists and installing $deb_name..."
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
    log_info "Installation complete!"
    log_info "Next steps:"
    echo "  1. Sign in from CLI anytime with: pve-gdrive login"
    echo "  2. Open the Proxmox Web UI -> Datacenter -> Storage -> Add -> Google Drive."
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
