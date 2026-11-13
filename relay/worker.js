/**
 * pve-storage-gdrive OAuth2 Callback Relay (Cloudflare Worker)
 * License: AGPL-3.0-or-later
 *
 * Stateless OAuth2 authorization code exchange relay.
 * - Never logs, persists, or caches authorization codes, refresh tokens, or access tokens.
 * - Exchanges Google OAuth2 authorization codes for tokens using Worker secrets:
 *     GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI
 * - Returns an XSS-escaped HTML page that posts `{ type: 'pve-gdrive-oauth', state, tokens }`
 *   to `window.opener` and provides a one-click Copy Authorization Payload fallback.
 */

const GOOGLE_TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token';

const SECURITY_HEADERS = {
  'Cache-Control': 'no-store, no-cache, must-revalidate, private',
  'Pragma': 'no-cache',
  'Expires': '0',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Content-Security-Policy':
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};

/**
 * Escape untrusted text for safe inclusion in HTML elements and attributes.
 * @param {unknown} value
 * @returns {string}
 */
export function escapeHtml(value) {
  const str = value == null ? '' : String(value);
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Serialize a value to JSON safe for embedding inside an HTML <script> block.
 * Escapes `<`, `>`, `&`, U+2028, and U+2029 to prevent `</script>` breakout.
 * @param {unknown} value
 * @returns {string}
 */
export function safeJsonForScript(value) {
  return JSON.stringify(value)
    .replace(/&/g, '\\u0026')
    .replace(/</g, '\\u003c')
    .replace(/>/g, '\\u003e')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

/**
 * Encode an OAuth payload object as UTF-8 safe Base64 for copy-paste fallback.
 * @param {unknown} payload
 * @returns {string}
 */
export function encodePayload(payload) {
  const json = JSON.stringify(payload);
  const bytes = new TextEncoder().encode(json);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Build the XSS-escaped HTML response for a successful OAuth exchange.
 * @param {{ state: string, tokens: Record<string, unknown> }} params
 * @returns {string}
 */
export function buildCallbackHtml({ state, tokens }) {
  const messagePayload = {
    type: 'pve-gdrive-oauth',
    state,
    tokens,
  };
  const b64Payload = encodePayload(messagePayload);
  const jsonForScript = safeJsonForScript(messagePayload);
  const escapedB64 = escapeHtml(b64Payload);

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Proxmox VE Google Drive Authorization</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 580px;
      margin: 40px auto;
      padding: 24px;
      line-height: 1.5;
    }
    .card {
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      padding: 24px;
    }
    h1 { font-size: 1.25rem; margin-top: 0; }
    .status {
      padding: 10px 12px;
      border-radius: 6px;
      background: #ecfdf5;
      color: #065f46;
      border: 1px solid #a7f3d0;
      margin-bottom: 16px;
    }
    .muted { color: #64748b; font-size: 0.9rem; }
    textarea {
      width: 100%;
      box-sizing: border-box;
      height: 110px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.8rem;
      padding: 8px;
      border-radius: 6px;
      border: 1px solid #94a3b8;
      margin: 8px 0 12px;
      resize: vertical;
    }
    button {
      background: #e57000;
      color: #ffffff;
      border: 0;
      border-radius: 6px;
      padding: 10px 16px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
    }
    button:hover { background: #cc6200; }
    #copy-status { margin-left: 10px; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Google Drive Authorization Complete</h1>
    <div id="status" class="status">Sending authorization to Proxmox VE...</div>
    <p class="muted">
      If this window does not close automatically or your browser blocked cross-window messaging,
      copy the authorization payload below and paste it into the Proxmox VE wizard.
    </p>
    <label for="payload"><strong>Authorization Payload</strong></label>
    <textarea id="payload" readonly>${escapedB64}</textarea>
    <div>
      <button id="copy-btn" type="button">Copy Authorization Payload</button>
      <span id="copy-status" role="status" aria-live="polite"></span>
    </div>
  </div>
  <script>
    (function () {
      var payload = ${jsonForScript};
      var statusEl = document.getElementById('status');
      var payloadEl = document.getElementById('payload');
      var copyBtn = document.getElementById('copy-btn');
      var copyStatus = document.getElementById('copy-status');

      var delivered = false;
      try {
        if (window.opener && !window.opener.closed) {
          window.opener.postMessage(payload, '*');
          delivered = true;
          statusEl.textContent = 'Authorization delivered to Proxmox VE. You may close this window.';
          setTimeout(function () {
            try { window.close(); } catch (_) {}
          }, 900);
        }
      } catch (_) {
        delivered = false;
      }

      if (!delivered) {
        statusEl.textContent = 'Automatic handoff unavailable (window.opener severed). Please copy the payload below into Proxmox VE.';
      }

      copyBtn.addEventListener('click', function () {
        var text = payloadEl.value;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            copyStatus.textContent = 'Copied!';
          }).catch(function () {
            fallbackCopy();
          });
        } else {
          fallbackCopy();
        }
      });

      function fallbackCopy() {
        payloadEl.focus();
        payloadEl.select();
        try {
          var ok = document.execCommand('copy');
          copyStatus.textContent = ok ? 'Copied!' : 'Press Ctrl+C to copy.';
        } catch (_) {
          copyStatus.textContent = 'Press Ctrl+C to copy.';
        }
      }
    })();
  </script>
</body>
</html>`;
}

/**
 * Build an XSS-escaped HTML error response.
 * @param {{ title: string, message: string, state?: string, error?: string }} params
 * @returns {string}
 */
export function buildErrorHtml({ title, message, state, error }) {
  const safeTitle = escapeHtml(title);
  const safeMessage = escapeHtml(message);
  const errorPayload = error
    ? safeJsonForScript({
        type: 'pve-gdrive-oauth',
        state: state || '',
        error,
        error_description: message,
      })
    : null;

  const openerScript = errorPayload
    ? `<script>
    (function () {
      try {
        if (window.opener && !window.opener.closed) {
          window.opener.postMessage(${errorPayload}, '*');
        }
      } catch (_) {}
    })();
  </script>`
    : '';

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${safeTitle}</title>
  <style>
    :root { color-scheme: light dark; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      max-width: 560px;
      margin: 40px auto;
      padding: 24px;
      line-height: 1.5;
    }
    .card {
      border: 1px solid #fca5a5;
      border-radius: 8px;
      padding: 24px;
    }
    h1 { font-size: 1.25rem; margin-top: 0; color: #b91c1c; }
    p { margin-bottom: 0; }
  </style>
</head>
<body>
  <div class="card">
    <h1>${safeTitle}</h1>
    <p>${safeMessage}</p>
  </div>
  ${openerScript}
</body>
</html>`;
}

function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      ...SECURITY_HEADERS,
    },
  });
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      ...SECURITY_HEADERS,
    },
  });
}

export default {
  /**
   * Cloudflare Worker fetch entrypoint.
   * @param {Request} request
   * @param {{ GOOGLE_CLIENT_ID?: string, GOOGLE_CLIENT_SECRET?: string, REDIRECT_URI?: string }} env
   * @returns {Promise<Response>}
   */
  async fetch(request, env = {}) {
    if (request.method !== 'GET') {
      return jsonResponse({ error: 'method_not_allowed' }, 405);
    }

    const url = new URL(request.url);

    if (url.pathname === '/health') {
      return jsonResponse({ status: 'ok', service: 'pve-storage-gdrive-relay' }, 200);
    }

    if (url.pathname !== '/oauth/callback') {
      return jsonResponse({ error: 'not_found' }, 404);
    }

    const params = url.searchParams;
    const oauthError = params.get('error');
    const errorDescription = params.get('error_description');
    const state = params.get('state') || '';
    const code = params.get('code');

    if (oauthError) {
      const detail = errorDescription
        ? `${oauthError}: ${errorDescription}`
        : oauthError;
      return htmlResponse(
        buildErrorHtml({
          title: 'Google Authorization Failed',
          message: detail,
          state,
          error: oauthError,
        }),
        400
      );
    }

    if (!code || !state) {
      return htmlResponse(
        buildErrorHtml({
          title: 'Invalid OAuth Callback',
          message: 'Missing required "code" or "state" query parameter.',
          state,
          error: 'invalid_request',
        }),
        400
      );
    }

    if (!env.GOOGLE_CLIENT_ID || !env.GOOGLE_CLIENT_SECRET) {
      return htmlResponse(
        buildErrorHtml({
          title: 'Relay Misconfigured',
          message: 'OAuth client credentials are not configured on this relay.',
          state,
          error: 'server_error',
        }),
        500
      );
    }

    const redirectUri = env.REDIRECT_URI || `${url.origin}/oauth/callback`;
    const tokenForm = new URLSearchParams({
      code,
      client_id: env.GOOGLE_CLIENT_ID,
      client_secret: env.GOOGLE_CLIENT_SECRET,
      redirect_uri: redirectUri,
      grant_type: 'authorization_code',
    });

    let tokenResponse;
    let tokenData;
    try {
      tokenResponse = await fetch(GOOGLE_TOKEN_ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Accept': 'application/json',
        },
        body: tokenForm.toString(),
      });
      tokenData = await tokenResponse.json();
    } catch (_) {
      return htmlResponse(
        buildErrorHtml({
          title: 'Token Exchange Failed',
          message: 'Unable to reach Google OAuth token endpoint.',
          state,
          error: 'token_exchange_failed',
        }),
        502
      );
    }

    if (!tokenResponse.ok || !tokenData || tokenData.error || !tokenData.access_token) {
      const errCode = (tokenData && tokenData.error) || 'token_exchange_failed';
      const errMsg =
        (tokenData && (tokenData.error_description || tokenData.error)) ||
        `Google token endpoint returned HTTP ${tokenResponse.status}.`;
      return htmlResponse(
        buildErrorHtml({
          title: 'Token Exchange Rejected',
          message: errMsg,
          state,
          error: errCode,
        }),
        400
      );
    }

    return htmlResponse(buildCallbackHtml({ state, tokens: tokenData }), 200);
  },
};
