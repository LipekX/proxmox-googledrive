#!/usr/bin/env bash
# Idempotent hook to inject/remove the pve-storage-gdrive Web UI script into
# Proxmox VE's index.html.tpl without modifying surrounding template lines.
# License: AGPL-3.0-or-later

set -euo pipefail

DEFAULT_TPL="/usr/share/pve-manager/index.html.tpl"
TARGET_TPL="${PVE_INDEX_TPL:-$DEFAULT_TPL}"

BEGIN_MARKER="<!-- # BEGIN pve-storage-gdrive -->"
SCRIPT_LINE='<script type="text/javascript" src="/pve2/js/pve-gdrive.js"></script>'
END_MARKER="<!-- # END pve-storage-gdrive -->"

usage() {
  cat <<'EOF'
Usage: overlay-hook.sh [--install | --remove | --status]

Environment:
  PVE_INDEX_TPL   Path to index.html.tpl (default: /usr/share/pve-manager/index.html.tpl)
EOF
}

has_begin_marker() {
  grep -Fq "# BEGIN pve-storage-gdrive" "$1"
}

has_end_marker() {
  grep -Fq "# END pve-storage-gdrive" "$1"
}

validate_markers() {
  local file="$1"
  local has_begin=0
  local has_end=0
  if has_begin_marker "$file"; then has_begin=1; fi
  if has_end_marker "$file"; then has_end=1; fi
  if (( has_begin != has_end )); then
    echo "Error: unbalanced pve-storage-gdrive markers in $file" >&2
    return 1
  fi
  return 0
}

write_preserving_attrs() {
  local src_tmp="$1"
  local dst="$2"
  # Writing through redirection preserves existing inode permissions and ownership.
  cat "$src_tmp" > "$dst"
}

do_remove() {
  local file="$1"
  if [[ ! -e "$file" ]]; then
    return 0
  fi
  validate_markers "$file"
  if ! has_begin_marker "$file"; then
    return 0
  fi

  local tmp
  tmp="$(mktemp "$(dirname "$file")/.pve-gdrive-overlay.XXXXXX")"
  trap 'rm -f "${tmp:-}"' RETURN

  local skipping=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if (( skipping == 0 )) && [[ "$line" == *"# BEGIN pve-storage-gdrive"* ]]; then
      skipping=1
      continue
    fi
    if (( skipping == 1 )); then
      if [[ "$line" == *"# END pve-storage-gdrive"* ]]; then
        skipping=0
      fi
      continue
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$file"

  write_preserving_attrs "$tmp" "$file"
}

do_install() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "Error: template file not found: $file" >&2
    return 1
  fi
  validate_markers "$file"

  # Fast-path idempotency check: exact block already installed.
  if has_begin_marker "$file" && grep -Fq "$SCRIPT_LINE" "$file"; then
    return 0
  fi

  # Remove any stale/older marker block first so upgrades remain clean.
  if has_begin_marker "$file"; then
    do_remove "$file"
  fi

  local tmp
  tmp="$(mktemp "$(dirname "$file")/.pve-gdrive-overlay.XXXXXX")"
  trap 'rm -f "${tmp:-}"' RETURN

  local inserted=0
  local has_body=0
  if grep -Eqi '</body>' "$file"; then
    has_body=1
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    if (( inserted == 0 )); then
      if (( has_body == 1 )) && [[ "${line,,}" == *"</body>"* ]]; then
        printf '%s\n%s\n%s\n' "$BEGIN_MARKER" "$SCRIPT_LINE" "$END_MARKER" >> "$tmp"
        inserted=1
      elif (( has_body == 0 )) && [[ "${line,,}" == *"</html>"* ]]; then
        printf '%s\n%s\n%s\n' "$BEGIN_MARKER" "$SCRIPT_LINE" "$END_MARKER" >> "$tmp"
        inserted=1
      fi
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$file"

  if (( inserted == 0 )); then
    printf '%s\n%s\n%s\n' "$BEGIN_MARKER" "$SCRIPT_LINE" "$END_MARKER" >> "$tmp"
  fi

  write_preserving_attrs "$tmp" "$file"
}

do_status() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "missing: $file"
    return 1
  fi
  if has_begin_marker "$file" && has_end_marker "$file" && grep -Fq "$SCRIPT_LINE" "$file"; then
    echo "installed"
    return 0
  fi
  echo "not-installed"
  return 1
}

main() {
  local action="${1:---install}"
  case "$action" in
    --install)
      do_install "$TARGET_TPL"
      ;;
    --remove)
      do_remove "$TARGET_TPL"
      ;;
    --status)
      do_status "$TARGET_TPL"
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $action" >&2
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
