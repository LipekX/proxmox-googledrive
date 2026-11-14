#!/usr/bin/env bash
# Idempotent hook to inject/remove the pve-storage-gdrive Web UI script into
# Proxmox VE's index.html.tpl without modifying surrounding template lines.
# License: AGPL-3.0-or-later

set -euo pipefail

DEFAULT_TPL="/usr/share/pve-manager/index.html.tpl"
TARGET_TPL="${PVE_INDEX_TPL:-$DEFAULT_TPL}"
DEFAULT_API2_PM="/usr/share/perl5/PVE/API2.pm"
TARGET_API2_PM="${PVE_API2_PM:-$DEFAULT_API2_PM}"

BEGIN_MARKER="<!-- # BEGIN pve-storage-gdrive -->"
SCRIPT_LINE='<script type="text/javascript" src="/pve2/js/pve-gdrive.js"></script>'
END_MARKER="<!-- # END pve-storage-gdrive -->"

PERL_BEGIN_MARKER="# BEGIN pve-storage-gdrive"
PERL_HOOK_LINE='eval { require PVE::Storage::Custom::GDrivePlugin; PVE::Storage::Custom::GDrivePlugin->register_api2(); };'
PERL_END_MARKER="# END pve-storage-gdrive"

usage() {
  cat <<'EOF'
Usage: overlay-hook.sh [--install | --remove | --status]

Environment:
  PVE_INDEX_TPL   Path to index.html.tpl (default: /usr/share/pve-manager/index.html.tpl)
  PVE_API2_PM     Path to PVE/API2.pm (default: /usr/share/perl5/PVE/API2.pm)
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

do_install_perl_api() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    return 0
  fi
  validate_markers "$file"

  if has_begin_marker "$file" && grep -Fq "$PERL_HOOK_LINE" "$file"; then
    return 0
  fi

  if has_begin_marker "$file"; then
    do_remove "$file"
  fi

  local tmp
  tmp="$(mktemp "$(dirname "$file")/.pve-gdrive-overlay.XXXXXX")"
  trap 'rm -f "${tmp:-}"' RETURN

  local lines=()
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    lines+=("$line")
  done < "$file"

  local last_idx=-1
  local i trimmed
  for (( i=${#lines[@]}-1; i>=0; i-- )); do
    trimmed="${lines[$i]#"${lines[$i]%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    if [[ "$trimmed" == "1;" ]]; then
      last_idx=$i
      break
    fi
  done

  for (( i=0; i<${#lines[@]}; i++ )); do
    if (( i == last_idx )); then
      printf '%s\n%s\n%s\n' "$PERL_BEGIN_MARKER" "$PERL_HOOK_LINE" "$PERL_END_MARKER" >> "$tmp"
    fi
    printf '%s\n' "${lines[$i]}" >> "$tmp"
  done

  if (( last_idx < 0 )); then
    printf '%s\n%s\n%s\n' "$PERL_BEGIN_MARKER" "$PERL_HOOK_LINE" "$PERL_END_MARKER" >> "$tmp"
  fi

  write_preserving_attrs "$tmp" "$file"
}

do_status() {
  local file="$1"
  local perl_file="${2:-}"
  if [[ ! -f "$file" ]]; then
    echo "missing: $file"
    return 1
  fi
  if has_begin_marker "$file" && has_end_marker "$file" && grep -Fq "$SCRIPT_LINE" "$file"; then
    if [[ -n "$perl_file" && -f "$perl_file" ]]; then
      if ! (has_begin_marker "$perl_file" && has_end_marker "$perl_file" && grep -Fq "$PERL_HOOK_LINE" "$perl_file"); then
        echo "not-installed"
        return 1
      fi
    fi
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
      do_install_perl_api "$TARGET_API2_PM"
      ;;
    --remove)
      do_remove "$TARGET_TPL"
      do_remove "$TARGET_API2_PM"
      ;;
    --status)
      do_status "$TARGET_TPL" "$TARGET_API2_PM"
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
