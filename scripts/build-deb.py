#!/usr/bin/env python3
"""Cross-distro Debian package (.deb) builder for pve-storage-gdrive.

Uses dpkg-deb when available (Debian/Proxmox) and falls back to tar + gzip + ar
with root numeric ownership and md5sums when building on non-Debian hosts
such as Fedora.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def parse_changelog_version(changelog_path: Path) -> str:
    """Extract the package version from the top entry of debian/changelog."""
    text = changelog_path.read_text(encoding="utf-8")
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    match = re.match(r"^[a-z0-9][a-z0-9+.-]*\s+\(([^)]+)\)", first_line)
    if not match:
        raise ValueError(f"Could not parse version from {changelog_path}: {first_line!r}")
    return match.group(1)


def parse_binary_control_stanza(control_path: Path) -> dict[str, str]:
    """Parse the binary package stanza from debian/control."""
    text = control_path.read_text(encoding="utf-8")
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key: str | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip():
            if current:
                stanzas.append(current)
                current = {}
                last_key = None
            continue
        if raw_line[0].isspace():
            if last_key is None:
                raise ValueError(f"Unexpected indented line in {control_path}: {raw_line!r}")
            current[last_key] = f"{current[last_key]}\n{raw_line}"
        elif ":" in raw_line:
            key, value = raw_line.split(":", 1)
            key = key.strip()
            current[key] = value.strip()
            last_key = key
        else:
            raise ValueError(f"Malformed line in {control_path}: {raw_line!r}")

    if current:
        stanzas.append(current)

    for stanza in stanzas:
        if "Package" in stanza:
            return stanza

    raise ValueError(f"No binary Package stanza found in {control_path}")


def collect_install_manifest(repo_root: Path) -> list[tuple[Path, str, int]]:
    """Return list of (source_path, package_rel_path, mode) for all payload files."""
    static_entries: list[tuple[str, str, int]] = [
        ("daemon/pve-gdrived", "usr/libexec/pve-gdrive/pve-gdrived", 0o755),
        ("daemon/pve-gdrive", "usr/bin/pve-gdrive", 0o755),
        (
            "perl/PVE/Storage/Custom/GDrivePlugin.pm",
            "usr/share/perl5/PVE/Storage/Custom/GDrivePlugin.pm",
            0o644,
        ),
        (
            "perl/PVE/API2/GDrive.pm",
            "usr/share/perl5/PVE/API2/GDrive.pm",
            0o644,
        ),
        (
            "scripts/vzdump-gdrive-hook.pl",
            "usr/libexec/pve-gdrive/vzdump-gdrive-hook.pl",
            0o755,
        ),
        ("ui/pve-gdrive.js", "usr/share/pve-manager/js/pve-gdrive.js", 0o644),
        (
            "ui/gdrive-oauth-callback.html",
            "usr/share/pve-manager/gdrive-oauth-callback.html",
            0o644,
        ),
        ("ui/overlay-hook.sh", "usr/libexec/pve-gdrive/overlay-hook.sh", 0o755),
        (
            "debian/pve-gdrived.service",
            "lib/systemd/system/pve-gdrived.service",
            0o644,
        ),
    ]

    manifest: list[tuple[Path, str, int]] = []
    missing: list[str] = []

    for src_rel, dst_rel, mode in static_entries:
        src_path = repo_root / src_rel
        if not src_path.is_file():
            missing.append(src_rel)
        else:
            manifest.append((src_path, dst_rel, mode))

    py_pkg_dir = repo_root / "daemon" / "pve_gdrive"
    if not py_pkg_dir.is_dir():
        missing.append("daemon/pve_gdrive/*.py")
    else:
        py_files = sorted(p for p in py_pkg_dir.glob("*.py") if p.is_file())
        if not py_files:
            missing.append("daemon/pve_gdrive/*.py")
        for py_file in py_files:
            manifest.append(
                (py_file, f"usr/libexec/pve-gdrive/pve_gdrive/{py_file.name}", 0o644)
            )

    if missing:
        raise FileNotFoundError(
            "Missing required package source file(s):\n  - " + "\n  - ".join(missing)
        )

    return manifest


def ensure_dir_tree(base_dir: Path, rel_path: str) -> None:
    """Create parent directories under base_dir with explicit 0755 permissions."""
    parts = Path(rel_path).parent.parts
    cur = base_dir
    for part in parts:
        cur = cur / part
        cur.mkdir(exist_ok=True)
        os.chmod(cur, 0o755)


def md5_file(path: Path) -> str:
    """Compute hexadecimal MD5 digest for a file."""
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    """Compute hexadecimal SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_payload_and_control(
    repo_root: Path,
    data_dir: Path,
    control_dir: Path,
    version_override: str | None,
) -> tuple[str, str, str]:
    """Stage data files and DEBIAN control metadata; return (pkg, ver, arch)."""
    manifest = collect_install_manifest(repo_root)
    stanza = parse_binary_control_stanza(repo_root / "debian" / "control")
    version = version_override or parse_changelog_version(
        repo_root / "debian" / "changelog"
    )
    pkg_name = stanza["Package"]
    arch = stanza.get("Architecture", "all")

    total_bytes = 0
    md5_lines: list[str] = []

    os.chmod(data_dir, 0o755)
    for src_path, dst_rel, mode in sorted(manifest, key=lambda item: item[1]):
        ensure_dir_tree(data_dir, dst_rel)
        dst_path = data_dir / dst_rel
        shutil.copyfile(src_path, dst_path)
        os.chmod(dst_path, mode)
        total_bytes += dst_path.stat().st_size
        md5_lines.append(f"{md5_file(dst_path)}  {dst_rel}")

    installed_size_kb = max(1, (total_bytes + 1023) // 1024)

    control_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(control_dir, 0o755)

    field_order = [
        "Package",
        "Version",
        "Architecture",
        "Maintainer",
        "Installed-Size",
        "Section",
        "Priority",
        "Depends",
        "Homepage",
        "Description",
    ]
    control_fields = dict(stanza)
    control_fields["Version"] = version
    control_fields["Installed-Size"] = str(installed_size_kb)

    rendered_lines: list[str] = []
    seen: set[str] = set()
    for key in field_order:
        if key in control_fields:
            rendered_lines.append(f"{key}: {control_fields[key]}")
            seen.add(key)
    for key, value in control_fields.items():
        if key not in seen:
            rendered_lines.append(f"{key}: {value}")

    control_file = control_dir / "control"
    control_file.write_text("\n".join(rendered_lines) + "\n", encoding="utf-8")
    os.chmod(control_file, 0o644)

    md5sums_file = control_dir / "md5sums"
    md5sums_file.write_text("\n".join(md5_lines) + "\n", encoding="utf-8")
    os.chmod(md5sums_file, 0o644)

    maintainer_files = [
        ("postinst", 0o755),
        ("prerm", 0o755),
        ("triggers", 0o644),
    ]
    for name, mode in maintainer_files:
        src = repo_root / "debian" / name
        if not src.is_file():
            raise FileNotFoundError(f"Missing maintainer file: debian/{name}")
        dst = control_dir / name
        shutil.copyfile(src, dst)
        os.chmod(dst, mode)

    return pkg_name, version, arch


def build_with_dpkg_deb(staging_dir: Path, output_deb: Path) -> None:
    """Build .deb using dpkg-deb."""
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(staging_dir), str(output_deb)],
        check=True,
    )


def build_with_tar_ar(
    data_dir: Path, control_dir: Path, work_dir: Path, output_deb: Path
) -> None:
    """Build .deb using tar + gzip + ar (cross-distro fallback)."""
    for tool in ("tar", "gzip", "ar"):
        if not shutil.which(tool):
            raise RuntimeError(f"Required build tool {tool!r} not found in PATH")

    debian_binary = work_dir / "debian-binary"
    debian_binary.write_text("2.0\n", encoding="ascii")
    os.chmod(debian_binary, 0o644)

    control_tar = work_dir / "control.tar.gz"
    data_tar = work_dir / "data.tar.gz"

    subprocess.run(
        [
            "tar",
            "--numeric-owner",
            "--owner=0",
            "--group=0",
            "-czf",
            str(control_tar),
            "-C",
            str(control_dir),
            ".",
        ],
        check=True,
    )
    subprocess.run(
        [
            "tar",
            "--numeric-owner",
            "--owner=0",
            "--group=0",
            "-czf",
            str(data_tar),
            "-C",
            str(data_dir),
            ".",
        ],
        check=True,
    )

    if output_deb.exists():
        output_deb.unlink()

    subprocess.run(
        [
            "ar",
            "-rc",
            str(output_deb.resolve()),
            "debian-binary",
            "control.tar.gz",
            "data.tar.gz",
        ],
        cwd=str(work_dir),
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build pve-storage-gdrive .deb package and dist/SHA256SUMS"
    )
    parser.add_argument(
        "--version",
        help="Override package version from debian/changelog",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Output directory for .deb and SHA256SUMS (default: dist)",
    )
    parser.add_argument(
        "--force-fallback",
        action="store_true",
        help="Force tar+gzip+ar builder even when dpkg-deb is installed",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    out_dir = (repo_root / args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pve-gdrive-deb-") as tmp:
        tmp_path = Path(tmp)
        use_dpkg_deb = bool(shutil.which("dpkg-deb")) and not args.force_fallback

        if use_dpkg_deb:
            staging_dir = tmp_path / "pkg"
            staging_dir.mkdir()
            control_dir = staging_dir / "DEBIAN"
            pkg_name, version, arch = stage_payload_and_control(
                repo_root, staging_dir, control_dir, args.version
            )
            deb_filename = f"{pkg_name}_{version}_{arch}.deb"
            deb_path = out_dir / deb_filename
            if deb_path.exists():
                deb_path.unlink()
            build_with_dpkg_deb(staging_dir, deb_path)
            backend = "dpkg-deb"
        else:
            data_dir = tmp_path / "data"
            control_dir = tmp_path / "control"
            work_dir = tmp_path / "ar_work"
            data_dir.mkdir()
            control_dir.mkdir()
            work_dir.mkdir()
            pkg_name, version, arch = stage_payload_and_control(
                repo_root, data_dir, control_dir, args.version
            )
            deb_filename = f"{pkg_name}_{version}_{arch}.deb"
            deb_path = out_dir / deb_filename
            build_with_tar_ar(data_dir, control_dir, work_dir, deb_path)
            backend = "tar+gzip+ar"

    sums_path = out_dir / "SHA256SUMS"
    digest = sha256_file(deb_path)
    sums_path.write_text(f"{digest}  {deb_filename}\n", encoding="utf-8")

    print(f"Built {deb_path} using {backend}")
    print(f"Wrote {sums_path}: {digest}  {deb_filename}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
