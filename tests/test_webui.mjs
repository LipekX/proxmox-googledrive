import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(__dirname, '..');

import worker, {
  escapeHtml,
  safeJsonForScript,
  encodePayload,
} from '../relay/worker.js';

// ---------------------------------------------------------------------------
// 1. Cloudflare Worker OAuth Relay tests
// ---------------------------------------------------------------------------

test('relay: GET /health returns ok JSON with no-store header', async () => {
  const res = await worker.fetch(new Request('https://auth.example.com/health'), {});
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type') || '', /application\/json/);
  assert.match(res.headers.get('cache-control') || '', /no-store/);
  const body = await res.json();
  assert.deepEqual(body, { status: 'ok', service: 'pve-storage-gdrive-relay' });
});

test('relay: unknown routes return 404 and non-GET returns 405', async () => {
  const res404 = await worker.fetch(new Request('https://auth.example.com/nope'), {});
  assert.equal(res404.status, 404);

  const res405 = await worker.fetch(
    new Request('https://auth.example.com/oauth/callback', { method: 'POST' }),
    {}
  );
  assert.equal(res405.status, 405);
});

test('relay: GET /oauth/callback exchanges code and returns postMessage + copyable payload HTML', async () => {
  const originalFetch = globalThis.fetch;
  let capturedUrl = null;
  let capturedBody = null;

  globalThis.fetch = async (url, init) => {
    capturedUrl = url;
    capturedBody = init?.body;
    return new Response(
      JSON.stringify({
        access_token: 'ya29.mock_access',
        refresh_token: '1//mock_refresh',
        expires_in: 3599,
        scope: 'https://www.googleapis.com/auth/drive.file',
        token_type: 'Bearer',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
  };

  try {
    const req = new Request(
      'https://auth.example.com/oauth/callback?code=auth_code_123&state=state_abc'
    );
    const res = await worker.fetch(req, {
      GOOGLE_CLIENT_ID: 'cid-test',
      GOOGLE_CLIENT_SECRET: 'csec-test',
      REDIRECT_URI: 'https://auth.example.com/oauth/callback',
    });

    assert.equal(res.status, 200);
    assert.equal(capturedUrl, 'https://oauth2.googleapis.com/token');
    const form = new URLSearchParams(capturedBody);
    assert.equal(form.get('code'), 'auth_code_123');
    assert.equal(form.get('client_id'), 'cid-test');
    assert.equal(form.get('client_secret'), 'csec-test');
    assert.equal(form.get('redirect_uri'), 'https://auth.example.com/oauth/callback');
    assert.equal(form.get('grant_type'), 'authorization_code');

    assert.match(res.headers.get('content-type') || '', /text\/html/);
    assert.match(res.headers.get('cache-control') || '', /no-store/);
    assert.equal(res.headers.get('x-frame-options'), 'DENY');
    assert.equal(res.headers.get('referrer-policy'), 'no-referrer');

    const html = await res.text();
    assert.match(html, /window\.opener\.postMessage\(payload,\s*'\*'\)/);
    assert.match(html, /Copy Authorization Payload/);

    const match = html.match(/<textarea id="payload" readonly>([^<]+)<\/textarea>/);
    assert.ok(match, 'expected copyable payload textarea');
    const decoded = JSON.parse(Buffer.from(match[1], 'base64').toString('utf8'));
    assert.equal(decoded.type, 'pve-gdrive-oauth');
    assert.equal(decoded.state, 'state_abc');
    assert.equal(decoded.tokens.access_token, 'ya29.mock_access');
    assert.equal(decoded.tokens.refresh_token, '1//mock_refresh');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('relay: XSS payloads in error, state, and tokens are strictly escaped', async () => {
  const xss = '</script><img src=x onerror=alert(1)>';
  assert.equal(
    escapeHtml('<script>"test"&\'x\'</script>'),
    '&lt;script&gt;&quot;test&quot;&amp;&#39;x&#39;&lt;/script&gt;'
  );
  const safeJson = safeJsonForScript({ s: xss });
  assert.ok(!safeJson.includes('</script>'), 'script breakout must be escaped');
  assert.ok(safeJson.includes('\\u003c/script\\u003e'));

  const errorReq = new Request(
    `https://auth.example.com/oauth/callback?error=${encodeURIComponent(xss)}&error_description=${encodeURIComponent('<svg onload=alert(2)>')}&state=${encodeURIComponent(xss)}`
  );
  const errorRes = await worker.fetch(errorReq, {});
  assert.equal(errorRes.status, 400);
  const errorHtml = await errorRes.text();
  assert.ok(!errorHtml.includes('<img src=x'), 'raw img tag must not appear in error HTML');
  assert.ok(!errorHtml.includes('<svg onload='), 'raw svg tag must not appear in error HTML');
  assert.ok(errorHtml.includes('&lt;/script&gt;&lt;img'));

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        access_token: 'tok',
        evil: xss,
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    );

  try {
    const okReq = new Request(
      `https://auth.example.com/oauth/callback?code=c1&state=${encodeURIComponent(xss)}`
    );
    const okRes = await worker.fetch(okReq, {
      GOOGLE_CLIENT_ID: 'cid',
      GOOGLE_CLIENT_SECRET: 'csec',
    });
    assert.equal(okRes.status, 200);
    const okHtml = await okRes.text();
    assert.ok(!okHtml.includes('<img src=x'), 'raw img tag must not appear in callback HTML');
    assert.ok(!okHtml.includes('</script><img'), 'script breakout must not appear in callback HTML');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('relay: missing params, missing secrets, and Google errors return proper status codes', async () => {
  const missingRes = await worker.fetch(
    new Request('https://auth.example.com/oauth/callback?code=onlycode'),
    { GOOGLE_CLIENT_ID: 'cid', GOOGLE_CLIENT_SECRET: 'csec' }
  );
  assert.equal(missingRes.status, 400);

  const misconfigRes = await worker.fetch(
    new Request('https://auth.example.com/oauth/callback?code=c&state=s'),
    {}
  );
  assert.equal(misconfigRes.status, 500);

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({ error: 'invalid_grant', error_description: 'Bad code' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } }
    );
  try {
    const rejectedRes = await worker.fetch(
      new Request('https://auth.example.com/oauth/callback?code=c&state=s'),
      { GOOGLE_CLIENT_ID: 'cid', GOOGLE_CLIENT_SECRET: 'csec' }
    );
    assert.equal(rejectedRes.status, 400);
    const text = await rejectedRes.text();
    assert.match(text, /Bad code/);
  } finally {
    globalThis.fetch = originalFetch;
  }

  globalThis.fetch = async () => {
    throw new Error('network down');
  };
  try {
    const netErrRes = await worker.fetch(
      new Request('https://auth.example.com/oauth/callback?code=c&state=s'),
      { GOOGLE_CLIENT_ID: 'cid', GOOGLE_CLIENT_SECRET: 'csec' }
    );
    assert.equal(netErrRes.status, 502);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ---------------------------------------------------------------------------
// 2. ExtJS Web UI Plugin tests (ui/pve-gdrive.js)
// ---------------------------------------------------------------------------

function loadWebUiWithMocks() {
  const definedClasses = new Map();
  const overrides = [];
  const apiCalls = [];

  function makeWidget(cfg = {}) {
    return {
      ...cfg,
      _hidden: Boolean(cfg.hidden),
      _disabled: Boolean(cfg.disabled),
      _value: cfg.value !== undefined ? cfg.value : '',
      _text: cfg.text || '',
      _data: cfg.data || [],
      setHidden(v) { this._hidden = Boolean(v); },
      setDisabled(v) { this._disabled = Boolean(v); },
      setValue(v) { this._value = v; },
      getValue() { return this._value; },
      setText(t) { this._text = t; },
      setData(d) { this._data = d; },
      isValid() { return true; },
      show() { this._shown = true; },
      close() { this._closed = true; },
    };
  }

  class BaseStoragePanel {
    constructor(cfg = {}) {
      Object.assign(this, cfg);
    }
    initComponent() {}
    setValues(v) { return v; }
    onGetValues(v) { return v; }
  }

  class BaseWindow {
    constructor(cfg = {}) {
      Object.assign(this, cfg);
    }
    initComponent() {}
    onDestroy() {}
  }

  class BaseContentView {
    constructor(cfg = {}) {
      Object.assign(this, cfg);
      this._toolbarItems = [];
      this._listeners = {};
      this.store = { reloaded: 0, reload() { this.reloaded += 1; } };
    }
    initComponent() {}
    getDockedItems() {
      const items = this._toolbarItems;
      return [{ add(...args) { items.push(...args); } }];
    }
    on(evt, fn) {
      this._listeners[evt] = fn;
    }
  }

  const Ext = {
    apply(target, src) {
      return Object.assign(target, src);
    },
    define(name, def) {
      const Parent =
        def.extend === 'PVE.panel.StorageBase'
          ? BaseStoragePanel
          : def.extend === 'Ext.window.Window'
            ? BaseWindow
            : Object;
      class Sub extends Parent {
        callParent(args) {
          if (args && args.length) {
            return super[this._currentMethod || 'initComponent']?.(...args);
          }
          return super.initComponent?.();
        }
      }
      for (const [k, v] of Object.entries(def)) {
        if (k === 'extend' || k === 'alias') continue;
        if (typeof v === 'function') {
          Sub.prototype[k] = function (...args) {
            this._currentMethod = k;
            return v.apply(this, args);
          };
        } else {
          Sub.prototype[k] = v;
        }
      }
      definedClasses.set(name, Sub);
      // Also attach to global namespace like ExtJS does
      const parts = name.split('.');
      let cur = sandbox;
      for (let i = 0; i < parts.length - 1; i += 1) {
        cur[parts[i]] = cur[parts[i]] || {};
        cur = cur[parts[i]];
      }
      cur[parts[parts.length - 1]] = Sub;
      return Sub;
    },
    create(name, cfg) {
      const Cls = definedClasses.get(name);
      if (Cls) {
        const inst = new Cls(cfg);
        inst.initComponent?.();
        return inst;
      }
      return makeWidget({ xtype: name, ...cfg });
    },
    override(target, patch) {
      overrides.push({ target, patch });
      for (const [k, v] of Object.entries(patch)) {
        const orig = target.prototype[k];
        target.prototype[k] = function (...args) {
          this.callParent = (a) => orig.apply(this, a || args);
          return v.apply(this, args);
        };
      }
    },
    String: {
      htmlEncode(value) {
        return String(value ?? '')
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;');
      },
    },
    Msg: {
      alert() {},
    },
  };

  const listeners = {};
  const sandbox = {
    Ext,
    gettext: (s) => s,
    URL,
    TextDecoder,
    Uint8Array,
    Buffer,
    location: { origin: 'https://pve.example.com:8006' },
    addEventListener(type, fn) {
      listeners[type] = fn;
    },
    removeEventListener(type, fn) {
      if (listeners[type] === fn) delete listeners[type];
    },
    open() {
      return { closed: false };
    },
    setInterval: () => 42,
    clearInterval: () => {},
    PVE: {
      Utils: { storageSchema: {} },
      panel: { StorageBase: BaseStoragePanel },
      storage: { ContentView: BaseContentView },
    },
    Proxmox: {
      Utils: {
        API2Request(opts) {
          apiCalls.push(opts);
          if (opts.url === '/api2/json/gdrive/accounts' && opts.success) {
            opts.success({
              result: {
                data: {
                  accounts: [
                    { id: 'personal', email: 'u@example.com', mode: 'relay', scope: 'drive.file' },
                  ],
                },
              },
            });
          } else if (opts.url === '/api2/json/gdrive/relay/config' && opts.method === 'GET' && opts.success) {
            opts.success({
              result: {
                data: {
                  enabled: false,
                  client_id: '',
                  has_client_secret: false,
                  redirect_uri: '',
                  scope: 'drive.file',
                },
              },
            });
          } else if (opts.url === '/api2/json/gdrive/relay/config' && opts.method === 'POST' && opts.success) {
            opts.success({
              result: {
                data: {
                  enabled: Boolean(opts.params && opts.params.enabled),
                  client_id: (opts.params && opts.params.client_id) || '',
                  has_client_secret: Boolean(opts.params && opts.params.client_secret),
                  redirect_uri: (opts.params && opts.params.redirect_uri) || '',
                  scope: (opts.params && opts.params.scope) || 'drive.file',
                },
              },
            });
          } else if (opts.url === '/api2/json/gdrive/oauth/start' && opts.success) {
            opts.success({
              result: {
                data: {
                  state: 'st-start-1',
                  auth_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=st-start-1',
                },
              },
            });
          } else if (opts.url === '/api2/json/gdrive/accounts/personal/drives' && opts.success) {
            opts.success({
              result: { data: { drives: [{ id: 'drv1', name: 'Team Backups' }] } },
            });
          } else if (opts.url === '/api2/json/gdrive/queue' && opts.success) {
            opts.success({
              result: {
                data: {
                  uploads: [],
                  storages: { gdrive1: { cache_used: 1024 * 1024, cache_max: 1024 * 1024 * 1024 } },
                },
              },
            });
          }
        },
      },
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox._listeners = listeners;

  const uiCode = fs.readFileSync(path.join(ROOT_DIR, 'ui/pve-gdrive.js'), 'utf8');
  vm.runInNewContext(uiCode, sandbox, { filename: 'ui/pve-gdrive.js' });

  return { sandbox, definedClasses, overrides, apiCalls, BaseContentView };
}

test('ui: registers gdrive storageSchema and GDriveInputPanel with expected fields & mappings', () => {
  const { sandbox, definedClasses, apiCalls } = loadWebUiWithMocks();

  const schema = sandbox.PVE.Utils.storageSchema.gdrive;
  assert.ok(schema, 'PVE.Utils.storageSchema.gdrive must be registered');
  assert.equal(schema.text, 'Google Drive');
  assert.equal(schema.ipanel, 'GDriveInputPanel');
  assert.equal(schema.faIcon, 'cloud');
  assert.equal(schema.backups, true);
  assert.equal(schema.selectExisting, false);
  assert.deepEqual(JSON.parse(JSON.stringify(schema.content)), {
    backup: true,
    iso: true,
    vztmpl: true,
  });

  assert.ok(definedClasses.has('PVE.storage.GDriveInputPanel'));
  assert.ok(definedClasses.has('PVE.gdrive.AuthWizardWindow'));
  assert.ok(definedClasses.has('PVE.gdrive.PickerModal'));

  const panel = sandbox.Ext.create('PVE.storage.GDriveInputPanel', { isCreate: true });

  const col1Names = panel.column1.map((c) => c.name).filter(Boolean);
  const col2Names = panel.column2.map((c) => c.name).filter(Boolean);
  const colBNames = panel.columnB.map((c) => c.name).filter(Boolean);

  assert.ok(col1Names.includes('account'));
  assert.ok(col1Names.includes('drive_id'));
  assert.ok(col1Names.includes('subfolder'));
  assert.ok(col2Names.includes('content'));
  assert.ok(col2Names.includes('wait_for_upload'));
  assert.ok(col2Names.includes('cache_dir'));
  assert.ok(col2Names.includes('cache_max_size'));
  assert.ok(colBNames.includes('encrypt'));
  assert.ok(colBNames.includes('crypt_passphrase'));

  const waitCb = panel.column2.find((c) => c.name === 'wait_for_upload');
  assert.equal(waitCb.checked, true, 'Wait for Google Drive upload on backup checked by default');

  // Verify onGetValues mapping for wait_for_upload <-> async_upload
  const syncOut = panel.onGetValues({
    account: 'personal',
    wait_for_upload: true,
    encrypt: 0,
    drive_id: '',
  });
  assert.equal(syncOut.async_upload, 0);
  assert.equal('wait_for_upload' in syncOut, false);
  assert.equal('drive_id' in syncOut, false);

  const asyncOut = panel.onGetValues({
    account: 'personal',
    wait_for_upload: false,
    encrypt: 1,
    crypt_passphrase: 'secret',
    crypt_passphrase_confirm: 'secret',
    drive_id: 'drv1',
  });
  assert.equal(asyncOut.async_upload, 1);
  assert.equal(asyncOut.encrypt, 1);
  assert.equal(asyncOut.crypt_passphrase, 'secret');
  assert.equal('crypt_passphrase_confirm' in asyncOut, false);

  // Verify setValues loads drives and maps async_upload back to wait_for_upload
  const loaded = panel.setValues({ account: 'personal', async_upload: 1, encrypt: 1 });
  assert.equal(loaded.wait_for_upload, false);
  assert.ok(apiCalls.some((c) => c.url === '/api2/json/gdrive/accounts/personal/drives'));
});

test('ui: ContentView hooks add Queue/Cache indicator and Import button for gdrive iso/vztmpl', () => {
  const { sandbox, apiCalls, BaseContentView } = loadWebUiWithMocks();
  const view = new BaseContentView({
    storage: 'gdrive1',
    nodename: 'pve1',
    content: 'iso',
    storageInfo: { type: 'gdrive', account: 'personal' },
  });
  view.initComponent();

  assert.ok(apiCalls.some((c) => c.url === '/api2/json/gdrive/queue'));
  assert.ok(apiCalls.some((c) => c.url === '/api2/json/gdrive/accounts'));
  assert.equal(view._toolbarItems.length, 3);
  const importBtn = view._toolbarItems[0];
  const queueText = view._toolbarItems[2];
  assert.equal(importBtn.text, 'Import from Google Drive');
  assert.equal(importBtn._hidden, false);
  assert.match(queueText._text, /Drive: Idle \| Cache: 1\.0 MiB \/ 1\.0 GiB/);

  // Cleanup timer on destroy
  view._listeners.destroy();
  assert.equal(view._gdriveQueueTimer, null);
});

test('ui: utils decodeOAuthPayload, extractAuthCode, and extractDriveFileIds work cleanly', () => {
  const { sandbox } = loadWebUiWithMocks();
  const { decodeOAuthPayload, extractAuthCode, extractDriveFileIds, formatBytes } =
    sandbox.PVE.gdrive.Utils;

  const b64 = encodePayload({
    type: 'pve-gdrive-oauth',
    state: 'st-1',
    tokens: { access_token: 'acc', refresh_token: 'ref' },
  });
  const decoded = decodeOAuthPayload(b64);
  assert.equal(decoded.state, 'st-1');
  assert.equal(decoded.tokens.refresh_token, 'ref');

  const rawJsonDecoded = decodeOAuthPayload('{"access_token":"a1"}');
  assert.equal(rawJsonDecoded.tokens.access_token, 'a1');

  assert.equal(
    extractAuthCode('https://localhost:8080/cb?code=4/0A_xyz&scope=drive'),
    '4/0A_xyz'
  );
  assert.equal(extractAuthCode('plain-code'), 'plain-code');

  assert.deepEqual(
    JSON.parse(
      JSON.stringify(
        extractDriveFileIds(
          'https://drive.google.com/file/d/1AbCdEfG/view?usp=sharing, https://drive.google.com/open?id=2ZyXwVu, 1AbCdEfG, 3PlainId'
        )
      )
    ),
    ['1AbCdEfG', '2ZyXwVu', '3PlainId']
  );

  assert.equal(formatBytes(1536), '1.5 KiB');
});

test('ui: AuthWizardWindow and PickerModal omit empty optional params instead of sending undefined', () => {
  const { sandbox, apiCalls } = loadWebUiWithMocks();

  const wizard = sandbox.Ext.create('PVE.gdrive.AuthWizardWindow', {});
  const tabs = wizard.items[0].items;

  // Tab 1: Relay paste-payload fallback with empty account_id
  const relayTabItems = tabs[0].items;
  const relayPayloadField = relayTabItems[4];
  const relayFinishBtn = relayTabItems[5];
  relayPayloadField.setValue(
    encodePayload({ type: 'pve-gdrive-oauth', state: 'st-relay', tokens: { access_token: 'tok1' } })
  );
  relayFinishBtn.handler();

  // Tab 2: BYOC complete with empty account_id
  const byocTabItems = tabs[1].items;
  const byocCodeField = byocTabItems[7];
  const byocFinishBtn = byocTabItems[8];
  wizard._byocState = 'st-byoc';
  byocCodeField.setValue('auth-code-xyz');
  byocFinishBtn.handler();

  // Tab 3: Service account with empty account_id
  const saTabItems = tabs[2].items;
  const saJsonField = saTabItems[1];
  const saSubmitBtn = saTabItems[2];
  saJsonField.setValue(
    JSON.stringify({
      type: 'service_account',
      client_email: 'sa@proj.iam.gserviceaccount.com',
      private_key: '-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n',
    })
  );
  saSubmitBtn.handler();

  // PickerModal with empty account & access_token
  const picker = sandbox.Ext.create('PVE.gdrive.PickerModal', {
    storeid: 'gdrive1',
    targetType: 'iso',
  });
  picker.items[3].setValue('file123');
  picker.buttons[0].handler();

  const relayCall = apiCalls.find(
    (c) => c.url === '/api2/json/gdrive/oauth/finish' && c.params.state === 'st-relay'
  );
  assert.ok(relayCall);
  assert.equal('account_id' in relayCall.params, false);

  const byocCall = apiCalls.find(
    (c) => c.url === '/api2/json/gdrive/oauth/finish' && c.params.state === 'st-byoc'
  );
  assert.ok(byocCall);
  assert.equal('account_id' in byocCall.params, false);

  const saCall = apiCalls.find((c) => c.url === '/api2/json/gdrive/accounts/service-account');
  assert.ok(saCall);
  assert.equal('account_id' in saCall.params, false);

  const pickerCall = apiCalls.find((c) => c.url === '/api2/json/gdrive/picker/import');
  assert.ok(pickerCall);
  assert.equal('account' in pickerCall.params, false);
  assert.equal('access_token' in pickerCall.params, false);
});

test('ui: self-hosted Proxmox OAuth relay config tab & same-origin callback page work end-to-end', () => {
  const { sandbox, apiCalls } = loadWebUiWithMocks();

  const wizard = sandbox.Ext.create('PVE.gdrive.AuthWizardWindow', {});
  const tabs = wizard.items[0].items;
  assert.equal(tabs.length, 4);
  assert.equal(tabs[3].title, 'Host OAuth Relay in Proxmox');

  // Verify GET relay/config was requested on init
  assert.ok(apiCalls.some((c) => c.url === '/api2/json/gdrive/relay/config' && c.method === 'GET'));

  // Configure self-hosted relay in Tab 4
  const selfItems = tabs[3].items;
  const selfEnabled = selfItems[1];
  const selfClientId = selfItems[2];
  const selfClientSecret = selfItems[3];
  const selfRedirectUri = selfItems[4];
  const selfSaveBtn = selfItems[7].items[0];

  assert.equal(
    selfRedirectUri.getValue(),
    'https://pve.example.com:8006/pve2/gdrive-oauth-callback.html'
  );
  selfEnabled.setValue(true);
  selfClientId.setValue('my-pve-client-id');
  selfClientSecret.setValue('my-pve-client-secret');
  selfSaveBtn.handler();

  const saveCall = apiCalls.find((c) => c.url === '/api2/json/gdrive/relay/config' && c.method === 'POST');
  assert.ok(saveCall);
  assert.equal(saveCall.params.enabled, 1);
  assert.equal(saveCall.params.client_id, 'my-pve-client-id');
  assert.equal(saveCall.params.client_secret, 'my-pve-client-secret');

  // Trigger Tab 1 Sign-In and simulate same-origin callback postMessage (pve-gdrive-oauth-code)
  const signInBtn = tabs[0].items[3].items[0];
  signInBtn.handler();

  const startCall = apiCalls.find((c) => c.url === '/api2/json/gdrive/oauth/start' && c.params.mode === 'relay');
  assert.ok(startCall);
  assert.equal(startCall.params.ui_origin, 'https://pve.example.com:8006');
  assert.equal(typeof sandbox._listeners.message, 'function');

  sandbox._listeners.message({
    data: { type: 'pve-gdrive-oauth-code', state: 'st-start-1', code: 'self-hosted-auth-code' },
  });

  const finishCodeCall = apiCalls.find(
    (c) => c.url === '/api2/json/gdrive/oauth/finish' && c.params.state === 'st-start-1'
  );
  assert.ok(finishCodeCall);
  assert.equal(finishCodeCall.params.code, 'self-hosted-auth-code');

  // Verify static callback HTML posts code back to window.opener same-origin without XSS
  const html = fs.readFileSync(path.join(ROOT_DIR, 'ui/gdrive-oauth-callback.html'), 'utf8');
  const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(scriptMatch);

  const elements = {
    status: { textContent: '', className: '' },
    detail: { textContent: '' },
    fallback: { className: 'hidden' },
    payloadBox: { value: '', select() {} },
    copyBtn: { textContent: 'Copy', addEventListener() {} },
    closeBtn: { addEventListener() {} },
  };
  const posted = [];
  const cbSandbox = {
    URLSearchParams,
    setTimeout() {},
    document: {
      getElementById: (id) => elements[id],
      execCommand() {},
    },
    navigator: {},
    window: {
      location: {
        origin: 'https://pve.example.com:8006',
        search: '?code=4%2F0TestCode&state=st-cb-1',
      },
      opener: {
        postMessage(msg, targetOrigin) {
          posted.push({ msg, targetOrigin });
        },
      },
      close() {},
    },
  };
  vm.runInNewContext(scriptMatch[1], cbSandbox, { filename: 'ui/gdrive-oauth-callback.html' });
  assert.equal(posted.length, 1);
  assert.equal(posted[0].targetOrigin, 'https://pve.example.com:8006');
  assert.equal(posted[0].msg.type, 'pve-gdrive-oauth-code');
  assert.equal(posted[0].msg.code, '4/0TestCode');
  assert.equal(posted[0].msg.state, 'st-cb-1');
  assert.equal(elements.payloadBox.value, '4/0TestCode');
});

// ---------------------------------------------------------------------------
// 3. overlay-hook.sh install / idempotency / remove tests
// ---------------------------------------------------------------------------

test('overlay-hook.sh: install is idempotent, preserves template & mode, and remove restores original', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pve-gdrive-hook-'));
  const tplPath = path.join(tmpDir, 'index.html.tpl');
  const hookPath = path.join(ROOT_DIR, 'ui/overlay-hook.sh');

  const originalTpl = [
    '<!DOCTYPE html>',
    '<html>',
    '<head>',
    '  <title>[% nodename %] - Proxmox Virtual Environment</title>',
    '  <script type="text/javascript" src="/pve2/js/pvemanagerlib.js"></script>',
    '</head>',
    '<body>',
    '  <div id="loading"></div>',
    '</body>',
    '</html>',
    '',
  ].join('\n');

  try {
    fs.writeFileSync(tplPath, originalTpl, { mode: 0o644 });
    const env = { ...process.env, PVE_INDEX_TPL: tplPath };

    // Install
    execFileSync(hookPath, ['--install'], { env });
    const afterInstall = fs.readFileSync(tplPath, 'utf8');
    assert.match(afterInstall, /# BEGIN pve-storage-gdrive/);
    assert.match(
      afterInstall,
      /<script type="text\/javascript" src="\/pve2\/js\/pve-gdrive\.js"><\/script>/
    );
    assert.match(afterInstall, /# END pve-storage-gdrive/);
    assert.ok(
      afterInstall.indexOf('pve-gdrive.js') < afterInstall.indexOf('</body>'),
      'script tag must appear before </body>'
    );
    assert.equal(fs.statSync(tplPath).mode & 0o777, 0o644);

    // Status
    const statusOut = execFileSync(hookPath, ['--status'], { env, encoding: 'utf8' }).trim();
    assert.equal(statusOut, 'installed');

    // Idempotency: run --install twice more
    execFileSync(hookPath, ['--install'], { env });
    execFileSync(hookPath, ['--install'], { env });
    const afterReinstall = fs.readFileSync(tplPath, 'utf8');
    assert.equal(afterReinstall, afterInstall);
    assert.equal((afterReinstall.match(/pve-gdrive\.js/g) || []).length, 1);

    // Remove
    execFileSync(hookPath, ['--remove'], { env });
    const afterRemove = fs.readFileSync(tplPath, 'utf8');
    assert.equal(afterRemove, originalTpl);

    // Idempotent remove
    execFileSync(hookPath, ['--remove'], { env });
    assert.equal(fs.readFileSync(tplPath, 'utf8'), originalTpl);

    // Remove on non-existent file exits 0
    execFileSync(hookPath, ['--remove'], {
      env: { ...process.env, PVE_INDEX_TPL: path.join(tmpDir, 'does-not-exist.tpl') },
    });

    // Install on non-existent file exits non-zero
    assert.throws(() => {
      execFileSync(hookPath, ['--install'], {
        env: { ...process.env, PVE_INDEX_TPL: path.join(tmpDir, 'does-not-exist.tpl') },
        stdio: 'pipe',
      });
    });
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
