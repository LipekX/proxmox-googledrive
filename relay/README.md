# pve-storage-gdrive OAuth2 Callback Relay

Stateless Cloudflare Worker that handles the Google OAuth2 callback (`GET /oauth/callback`) for Proxmox VE nodes running `pve-storage-gdrive`.

## Privacy & Security Guarantees

- **Stateless**: Uses no KV, D1, R2, or Durable Objects.
- **No Token Logging**: Never logs authorization codes, access tokens, refresh tokens, or state values (`[observability] enabled = false`).
- **XSS & Injection Hardened**: Escapes all dynamic HTML values and script-embedded JSON, sets strict `Content-Security-Policy`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`.
- **Fallback Delivery**: Posts `{ type: 'pve-gdrive-oauth', state, tokens }` via `window.opener.postMessage(..., '*')` and provides a one-click **Copy Authorization Payload** fallback if `window.opener` is severed by browser privacy settings.

## Endpoints

- `GET /health` -> `200 OK` JSON `{ "status": "ok", "service": "pve-storage-gdrive-relay" }`
- `GET /oauth/callback?code=...&state=...` -> Exchanges `code` at `https://oauth2.googleapis.com/token` and returns the handoff HTML page.

## Self-Hosting Instructions

### 1. Create Google Cloud OAuth2 Credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Enable the **Google Drive API**.
3. Configure the **OAuth consent screen** (External or Internal) and add the `https://www.googleapis.com/auth/drive.file` scope (and optionally `https://www.googleapis.com/auth/drive`).
4. Create an **OAuth client ID** of type **Web application**.
5. Add your Worker callback URL under **Authorized redirect URIs**, for example:
   `https://pve-gdrive-relay.<your-subdomain>.workers.dev/oauth/callback`
   (or your custom domain `https://auth.example.com/oauth/callback`).

### 2. Configure & Deploy with Wrangler

```bash
cd relay
npx wrangler login
```

Edit `wrangler.toml` and update `REDIRECT_URI` to match your exact callback URL, then set your Google OAuth secrets:

```bash
npx wrangler secret put GOOGLE_CLIENT_ID
npx wrangler secret put GOOGLE_CLIENT_SECRET
npx wrangler deploy
```

Verify the deployment:

```bash
curl -fsS https://<your-worker-domain>/health
```

### 3. Point Proxmox VE at Your Self-Hosted Relay

When adding an account via the **Own OAuth App (BYOC)** tab in the Proxmox VE Web UI, or when calling `POST /api2/json/gdrive/oauth/start`, pass your Worker's `redirect_uri` along with your `client_id` and `client_secret` (or configure `PVE_GDRIVE_RELAY_URL` in the `pve-gdrived` daemon environment).
