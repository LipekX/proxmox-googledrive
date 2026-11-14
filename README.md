# pve-storage-gdrive

Native **Google Drive** storage backend plugin for **Proxmox VE 8.x and 9.x** (AGPL-3.0-or-later).

Mount your Google **My Drive** or **Shared Drives** as a first-class Proxmox storage target for `vzdump` backups, ISO images, and LXC container templates — complete with a Web UI wizard, Easy Google Sign-In, optional `rclone crypt` client-side encryption, and synchronous upload verification for `vzdump`.

---

## Quick Install

Run on each Proxmox VE node as `root`:

```bash
bash -c "$(curl -fsSL https://install.lipa.run/proxmox-gdrive.sh)"
```

### Installer Options

```bash
# Install a specific release version
bash -c "$(curl -fsSL https://install.lipa.run/proxmox-gdrive.sh)" -- --version 0.1.0-2

# Install from a custom .deb URL (still verifies SHA256SUMS)
bash -c "$(curl -fsSL https://install.lipa.run/proxmox-gdrive.sh)" -- --deb-url https://example.com/pve-storage-gdrive_0.1.0-2_all.deb

# Non-interactive install
bash -c "$(curl -fsSL https://install.lipa.run/proxmox-gdrive.sh)" -- --yes

# Uninstall (preserves /etc/pve/priv/gdrive credentials and remote Drive files)
bash -c "$(curl -fsSL https://install.lipa.run/proxmox-gdrive.sh)" -- --uninstall
```

### Local Install (from `git clone`)

If you want to build and install directly from a local checkout:

```bash
git clone https://github.com/LipekX/proxmox-gdrive.git
cd proxmox-gdrive
sudo ./local_install.sh
```

Options: `--yes` (non-interactive), `--skip-build` (use existing `.deb` in `dist/`), `--uninstall`.

---

## One-Time Google Sign-In (CLI or Web UI — No Cloud Console Setup Needed)

`pve-storage-gdrive` lets you log in **once** with your normal Google account using the built-in desktop OAuth client — no Google Cloud project, no Service Account IAM setup, and no periodic re-logins.

### Option 1: Sign in via CLI (during install or anytime)

Run on your Proxmox node:

```bash
pve-gdrive login
```

1. Open the printed Google Sign-In link in any browser on your computer or phone and click **Allow**.
2. Your browser will redirect to `http://127.0.0.1:53682/?code=...` (even if the browser says *Unable to connect*, that is normal because your browser is not running on the Proxmox server).
3. Copy the full `http://127.0.0.1:53682/?code=...` URL from your browser's address bar and paste it into the terminal prompt.

### Option 2: Sign in via Proxmox Web UI

1. Open the Proxmox Web UI -> **Datacenter -> Storage -> Add -> Google Drive**.
2. Click **Sign in with Google** -> **1. Open Google Sign-In in Browser**.
3. Copy the redirected `http://127.0.0.1:53682/?code=...` URL from your browser's address bar and paste it into the dialog.

*(Power users can also paste the output of `rclone authorize "drive"` or a Service Account JSON key into the same field.)*

Account profiles (with your long-lived `refresh_token`) are stored atomically in `/etc/pve/priv/gdrive/accounts/<id>.json` (protected by `pmxcfs` `/etc/pve/priv`). Short-lived access tokens are refreshed automatically in memory by `pve-gdrived`, so routine token refreshes never churn `/etc/pve` (`pmxcfs`).

---

## Google Drive `proxmox/` Folder Layout

On storage initialization, `pve-gdrived` resolves or creates a single `proxmox/` root folder (or an optional configured subfolder inside `proxmox/`) and pins its Google Drive `folder_id` (`root_folder_id`) to prevent duplicate folders:

```text
Google Drive (My Drive or Shared Drive)
└── proxmox/                      # Pinned by folder_id
    ├── dump/                     # Proxmox vzdump backups (backup)
    └── template/
        ├── iso/                  # ISO images (iso)
        └── cache/                # LXC container templates (vztmpl)
```

Supported Proxmox content types are strictly `backup`, `iso`, and `vztmpl` (`images` and `rootdir` VM/CT disks are intentionally excluded).

### Optional Client-Side Encryption (`rclone crypt`)

Enable **Client-Side Encryption** when adding a storage to wrap the Google Drive remote in an `rclone crypt` layer. The passphrase is stored per-storage under `/etc/pve/priv/gdrive/storages/<storeid>.crypt.json` (`0600`). Back up this passphrase independently — without it, encrypted backups in Drive cannot be recovered.

---

## `vzdump` Synchronous Upload Hook

By default (`async_upload: 0`), Proxmox backups staged through the `rclone` VFS full cache (`/var/cache/pve-gdrive`) are verified before `vzdump` reports success:

- Configure `/etc/vzdump.conf` (or pass `--script` to `vzdump`):
  ```text
  script: /usr/libexec/pve-gdrive/vzdump-gdrive-hook.pl
  ```
- On the `backup-end` phase, `vzdump-gdrive-hook.pl` checks whether the target storage uses `type: gdrive` and calls `POST /v1/uploads/wait` on `/run/pve-gdrived.sock`.
- If the upload to Google Drive fails or times out, the hook exits non-zero so `vzdump` fails loudly instead of reporting a false-positive backup.
- Set `async_upload 1` on the storage definition if you prefer `vzdump` to return immediately while `rclone` uploads in the background.
- When `cache_max_size` is unset, `pve-gdrived` calculates a conservative auto-cap of `min(20G, 25% of free space)` on `cache_dir`.

---

## Proxmox Cluster Setup

In a Proxmox VE cluster:

1. `/etc/pve/storage.cfg` and `/etc/pve/priv/gdrive/` automatically replicate across all cluster nodes via `pmxcfs`.
2. **Run the installer on every node in the cluster** so that `pve-gdrived`, the Perl storage/API plugins (`GDrivePlugin.pm`, `GDrive.pm`), `vzdump-gdrive-hook.pl`, and the Web UI overlay are present on every host.
3. Each node runs its own local `pve-gdrived.service` and mounts active storages at `/mnt/pve/<storeid>`.

Verify daemon and upload queue status from the CLI on any node:

```bash
pve-gdrive status
pve-gdrive accounts
pve-gdrive queue
```

---

## Building from Source

`pve-storage-gdrive` is `Architecture: all` (pure Python 3.11+ standard library daemon + Perl + ExtJS) and can be packaged on both Debian/Proxmox (`dpkg-deb`) and non-Debian Linux hosts such as Fedora (`tar` + `gzip` + `ar`):

```bash
# Run all test suites and syntax checks
make test

# Build dist/pve-storage-gdrive_<version>_all.deb and dist/SHA256SUMS
make deb

# Standard Debian package build (on Debian/Proxmox hosts)
dpkg-buildpackage -us -uc -b

# Clean build artifacts
make clean
```

---

## Disclaimers

- **Unofficial Community Project**: `pve-storage-gdrive` is an independent open-source plugin and is **not affiliated with, endorsed by, or supported by Proxmox Server Solutions GmbH or Google LLC**. "Proxmox" and "Google Drive" are trademarks of their respective owners.
- **No Warranty / Backup Responsibility**: This software is provided **AS-IS** under the `AGPL-3.0-or-later` license without warranty of any kind. Cloud FUSE mounts depend on internet connectivity, Google Drive API availability/rate limits, and `rclone`. **Always test backup restores periodically** and maintain independent local or offsite backups for critical workloads.
- **Client-Side Encryption (`rclone crypt`)**: If you enable encryption, you are solely responsible for backing up your passphrase (also stored in `/etc/pve/priv/gdrive/storages/<storeid>.crypt.json`). Lost passphrases cannot be recovered.
- **Shared OAuth Client & API Quotas**: The built-in Easy Google Sign-In flow uses `rclone`'s public desktop OAuth client (`drive` scope), whose Google Drive API request quota is shared across users. For heavy production workloads or high request rates, consider using your own Google Cloud OAuth client or Service Account.
- **Cluster & Credential Security**: Account tokens and encryption passphrases are stored with `0600` permissions in `/etc/pve/priv/gdrive/` (replicated across cluster nodes via `pmxcfs`). Anyone with `root` access on a cluster node can access those credentials.

---

## License

Licensed under the **GNU Affero General Public License v3.0 or later** (`AGPL-3.0-or-later`). See [LICENSE](LICENSE) for the full license text.
