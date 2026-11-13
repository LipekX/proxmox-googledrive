/*
 * Proxmox VE Web UI plugin for Google Drive Storage (pve-storage-gdrive)
 * License: AGPL-3.0-or-later
 */

(function (global) {
    'use strict';

    var Ext = global.Ext;
    var PVE = (global.PVE = global.PVE || {});
    var Proxmox = (global.Proxmox = global.Proxmox || {});
    var _ = typeof global.gettext === 'function' ? global.gettext : function (s) { return s; };

    PVE.Utils = PVE.Utils || {};
    PVE.Utils.storageSchema = PVE.Utils.storageSchema || {};
    PVE.storage = PVE.storage || {};
    PVE.gdrive = PVE.gdrive || {};

    /**
     * Register Google Drive storage type in Proxmox VE storageSchema.
     */
    PVE.Utils.storageSchema.gdrive = {
        name: 'Google Drive',
        text: 'Google Drive',
        ipanel: 'GDriveInputPanel',
        faIcon: 'cloud',
        backups: true,
        selectExisting: false,
        content: {
            backup: true,
            iso: true,
            vztmpl: true,
        },
    };

    /**
     * Pure utility helpers exposed on PVE.gdrive.Utils for testability and reuse.
     */
    var Utils = (PVE.gdrive.Utils = {
        /**
         * Decode a base64 or JSON authorization payload copied from the OAuth relay.
         * Returns `{ state, tokens }`.
         * @param {string} rawInput
         * @returns {{ state: string, tokens: Record<string, unknown> }}
         */
        decodeOAuthPayload: function (rawInput) {
            if (!rawInput || typeof rawInput !== 'string') {
                throw new Error('Authorization payload is empty.');
            }
            var trimmed = rawInput.trim();
            var parsed = null;

            if (trimmed.charAt(0) === '{') {
                parsed = JSON.parse(trimmed);
            } else {
                var normalized = trimmed.replace(/-/g, '+').replace(/_/g, '/');
                var decodedJson;
                if (typeof global.atob === 'function') {
                    var binary = global.atob(normalized);
                    var bytes = new Uint8Array(binary.length);
                    for (var i = 0; i < binary.length; i += 1) {
                        bytes[i] = binary.charCodeAt(i);
                    }
                    decodedJson =
                        typeof TextDecoder !== 'undefined'
                            ? new TextDecoder().decode(bytes)
                            : binary;
                } else if (typeof Buffer !== 'undefined') {
                    decodedJson = Buffer.from(normalized, 'base64').toString('utf8');
                } else {
                    throw new Error('Base64 decoder unavailable.');
                }
                parsed = JSON.parse(decodedJson);
            }

            if (!parsed || typeof parsed !== 'object') {
                throw new Error('Invalid authorization payload.');
            }
            if (parsed.tokens && typeof parsed.tokens === 'object') {
                return {
                    state: parsed.state ? String(parsed.state) : '',
                    tokens: parsed.tokens,
                };
            }
            if (parsed.access_token || parsed.refresh_token) {
                return {
                    state: '',
                    tokens: parsed,
                };
            }
            throw new Error('Payload does not contain OAuth tokens.');
        },

        /**
         * Extract an OAuth authorization code from either a raw code string
         * or a pasted redirect URL (`?code=...`).
         * @param {string} rawInput
         * @returns {string}
         */
        extractAuthCode: function (rawInput) {
            if (!rawInput || typeof rawInput !== 'string') {
                return '';
            }
            var trimmed = rawInput.trim();
            if (/^https?:\/\//i.test(trimmed)) {
                try {
                    var url = new URL(trimmed);
                    return url.searchParams.get('code') || '';
                } catch (_) {
                    return trimmed;
                }
            }
            return trimmed;
        },

        /**
         * Extract Google Drive file IDs from comma/newline separated IDs or Drive URLs.
         * @param {string} rawInput
         * @returns {string[]}
         */
        extractDriveFileIds: function (rawInput) {
            if (!rawInput || typeof rawInput !== 'string') {
                return [];
            }
            var tokens = rawInput
                .split(/[\s,]+/)
                .map(function (s) { return s.trim(); })
                .filter(Boolean);

            var ids = [];
            var seen = Object.create(null);
            for (var i = 0; i < tokens.length; i += 1) {
                var tok = tokens[i];
                var id = tok;
                var fileMatch = tok.match(/\/file\/d\/([A-Za-z0-9_-]+)/);
                var openMatch = tok.match(/[?&]id=([A-Za-z0-9_-]+)/);
                if (fileMatch) {
                    id = fileMatch[1];
                } else if (openMatch) {
                    id = openMatch[1];
                }
                if (id && !seen[id]) {
                    seen[id] = true;
                    ids.push(id);
                }
            }
            return ids;
        },

        /**
         * Format byte counts cleanly for toolbar status indicators.
         * @param {number} bytes
         * @returns {string}
         */
        formatBytes: function (bytes) {
            var num = Number(bytes);
            if (!isFinite(num) || num < 0) {
                return '0 B';
            }
            var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
            var idx = 0;
            while (num >= 1024 && idx < units.length - 1) {
                num /= 1024;
                idx += 1;
            }
            return (idx === 0 ? String(Math.round(num)) : num.toFixed(1)) + ' ' + units[idx];
        },

        /**
         * Execute a Proxmox API2 JSON request with fallback for test environments.
         * @param {object} opts
         */
        apiRequest: function (opts) {
            if (Proxmox && Proxmox.Utils && typeof Proxmox.Utils.API2Request === 'function') {
                return Proxmox.Utils.API2Request(opts);
            }
            throw new Error('Proxmox.Utils.API2Request is not available.');
        },

        /**
         * Determine whether a storage record represents a Google Drive storage.
         * @param {object} contentView
         * @returns {boolean}
         */
        isGDriveStorage: function (contentView) {
            if (!contentView) {
                return false;
            }
            if (contentView.storageInfo && contentView.storageInfo.type === 'gdrive') {
                return true;
            }
            if (
                contentView.nodename &&
                contentView.storage &&
                PVE.data &&
                PVE.data.ResourceStore &&
                typeof PVE.data.ResourceStore.getById === 'function'
            ) {
                var rec = PVE.data.ResourceStore.getById(
                    'storage/' + contentView.nodename + '/' + contentView.storage
                );
                if (rec && rec.data && rec.data.plugintype === 'gdrive') {
                    return true;
                }
            }
            return false;
        },
    });

    if (!Ext || typeof Ext.define !== 'function') {
        return;
    }

    /**
     * 3-Tab Google Drive Account Authorization Wizard Modal:
     *   1. 1-Click Google Sign-In (Relay popup + postMessage + paste-payload fallback)
     *   2. Own OAuth App (BYOC)
     *   3. Service Account JSON
     */
    Ext.define('PVE.gdrive.AuthWizardWindow', {
        extend: 'Ext.window.Window',
        alias: 'widget.pveGDriveAuthWizard',

        title: _('Connect Google Drive Account'),
        modal: true,
        width: 600,
        bodyPadding: 10,
        resizable: false,

        onAccountCreated: null,
        _oauthState: null,
        _byocState: null,
        _messageHandler: null,

        initComponent: function () {
            var me = this;

            var uiOrigin =
                global.location && global.location.origin ? String(global.location.origin) : '';
            var defaultCallbackUri = uiOrigin ? uiOrigin + '/pve2/gdrive-oauth-callback.html' : '';

            var relayModeBadge = Ext.create('Ext.form.field.Display', {
                fieldLabel: _('Active Relay'),
                value: _('Checking relay configuration...'),
            });

            var relayStatusField = Ext.create('Ext.form.field.Display', {
                fieldLabel: _('Status'),
                value: _('Click "Sign in with Google" to authorize access.'),
            });

            var relayAccountIdField = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Account Label'),
                name: 'relay_account_id',
                emptyText: _('Optional (auto-detected from Google email)'),
            });

            var relayPayloadField = Ext.create('Ext.form.field.TextArea', {
                fieldLabel: _('Paste Payload / Code'),
                name: 'relay_payload',
                height: 90,
                emptyText: _('Fallback: paste the Authorization Payload or Code copied from the callback window if automatic handoff failed.'),
            });

            var finishOAuth = function (extraParams, statusTarget) {
                if (statusTarget) {
                    statusTarget.setValue(_('Completing authorization...'));
                }
                Utils.apiRequest({
                    url: '/api2/json/gdrive/oauth/finish',
                    method: 'POST',
                    params: extraParams,
                    waitMsgTarget: me,
                    success: function (response) {
                        var account =
                            (response.result && response.result.data && response.result.data.account) ||
                            (response.result && response.result.data) ||
                            null;
                        if (typeof me.onAccountCreated === 'function') {
                            me.onAccountCreated(account);
                        }
                        me.close();
                    },
                    failure: function (response) {
                        var msg = response.htmlStatus || _('Authorization failed.');
                        if (statusTarget) {
                            statusTarget.setValue(_('Error: ') + msg);
                        }
                        Ext.Msg.alert(_('Error'), msg);
                    },
                });
            };

            var finishRelayPayload = function (state, tokens, accountId) {
                var params = {
                    state: state,
                    tokens_json: JSON.stringify(tokens),
                };
                var trimmedAccountId = accountId ? String(accountId).trim() : '';
                if (trimmedAccountId) {
                    params.account_id = trimmedAccountId;
                }
                finishOAuth(params, relayStatusField);
            };

            var finishOAuthCode = function (state, code, accountId, statusTarget) {
                var params = {
                    state: state,
                    code: code,
                };
                var trimmedAccountId = accountId ? String(accountId).trim() : '';
                if (trimmedAccountId) {
                    params.account_id = trimmedAccountId;
                }
                finishOAuth(params, statusTarget);
            };

            var byocAccountId = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Account Label'),
                emptyText: _('Optional (auto-detected from Google email)'),
            });
            var byocClientId = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Client ID'),
                allowBlank: false,
            });
            var byocClientSecret = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Client Secret'),
                inputType: 'password',
                allowBlank: false,
            });
            var byocScope = Ext.create('Ext.form.field.ComboBox', {
                fieldLabel: _('OAuth Scope'),
                store: [
                    ['drive.file', _('drive.file (Recommended - Per-file access)')],
                    ['drive', _('drive (Full Google Drive access)')],
                ],
                value: 'drive.file',
                editable: false,
                forceSelection: true,
                queryMode: 'local',
            });
            var byocRedirectUri = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Redirect URI'),
                emptyText: _('Optional (defaults to local/relay URI)'),
            });
            var byocAuthUrlDisplay = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Auth URL'),
                readOnly: true,
                hidden: true,
            });
            var byocCodeField = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Auth Code / URL'),
                emptyText: _('Paste authorization code or full callback URL'),
            });

            var saAccountId = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Account Label'),
                emptyText: _('Optional (auto-detected from service account email)'),
            });
            var saJsonField = Ext.create('Ext.form.field.TextArea', {
                fieldLabel: _('Service Account JSON'),
                height: 190,
                allowBlank: false,
                emptyText: _('Paste Google Cloud Service Account JSON key contents...'),
            });

            var selfRelayEnabled = Ext.create('Ext.form.field.Checkbox', {
                fieldLabel: _('Enable Self-Hosted Relay'),
                boxLabel: _('Use this Proxmox UI as the OAuth relay for 1-Click Sign-In'),
                inputValue: true,
                uncheckedValue: false,
            });
            var selfRelayClientId = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Google Client ID'),
                emptyText: _('e.g. 1234567890-abc.apps.googleusercontent.com'),
            });
            var selfRelayClientSecret = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Google Client Secret'),
                inputType: 'password',
                emptyText: _('Required on initial setup; leave blank to keep saved secret'),
            });
            var selfRelayRedirectUri = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Callback Redirect URI'),
                value: defaultCallbackUri,
                emptyText: defaultCallbackUri || _('https://<pve-host>:8006/pve2/gdrive-oauth-callback.html'),
            });
            var selfRelayScope = Ext.create('Ext.form.field.ComboBox', {
                fieldLabel: _('Default OAuth Scope'),
                store: [
                    ['drive.file', _('drive.file (Recommended - Per-file access)')],
                    ['drive', _('drive (Full Google Drive access)')],
                ],
                value: 'drive.file',
                editable: false,
                forceSelection: true,
                queryMode: 'local',
            });
            var selfRelayStatusDisplay = Ext.create('Ext.form.field.Display', {
                fieldLabel: _('Secret Status'),
                value: _('Not configured'),
            });

            var updateRelayStatusUI = function (cfg) {
                var data = cfg || {};
                selfRelayEnabled.setValue(Boolean(data.enabled));
                if (data.client_id) {
                    selfRelayClientId.setValue(data.client_id);
                }
                if (data.redirect_uri) {
                    selfRelayRedirectUri.setValue(data.redirect_uri);
                } else if (defaultCallbackUri && !selfRelayRedirectUri.getValue()) {
                    selfRelayRedirectUri.setValue(defaultCallbackUri);
                }
                if (data.scope) {
                    selfRelayScope.setValue(data.scope);
                }
                selfRelayStatusDisplay.setValue(
                    data.has_client_secret
                        ? _('Client secret stored in /etc/pve/priv/gdrive/relay.json')
                        : _('No client secret stored')
                );
                if (data.enabled) {
                    relayModeBadge.setValue(
                        Ext.String.htmlEncode(
                            _('Self-Hosted in Proxmox UI (') +
                                (data.redirect_uri || defaultCallbackUri || '/pve2/gdrive-oauth-callback.html') +
                                ')'
                        )
                    );
                } else {
                    relayModeBadge.setValue(_('External Cloud Relay (default) — or configure Self-Hosted tab'));
                }
            };

            var attachOAuthMessageListener = function (expectedMode) {
                me._detachMessageListener();
                me._messageHandler = function (event) {
                    var msg = event && event.data;
                    if (!msg || (msg.type !== 'pve-gdrive-oauth' && msg.type !== 'pve-gdrive-oauth-code')) {
                        return;
                    }
                    var expectedState = expectedMode === 'byoc' ? me._byocState : me._oauthState;
                    if (expectedState && msg.state && msg.state !== expectedState) {
                        return;
                    }
                    me._detachMessageListener();
                    if (msg.error) {
                        if (expectedMode === 'byoc') {
                            Ext.Msg.alert(_('Error'), _('Authorization denied: ') + msg.error);
                        } else {
                            relayStatusField.setValue(_('Authorization denied: ') + msg.error);
                        }
                        return;
                    }
                    if (msg.type === 'pve-gdrive-oauth' && msg.tokens) {
                        finishRelayPayload(
                            msg.state || expectedState,
                            msg.tokens,
                            relayAccountIdField.getValue()
                        );
                    } else if (msg.type === 'pve-gdrive-oauth-code' && msg.code) {
                        if (expectedMode === 'byoc') {
                            byocCodeField.setValue(msg.code);
                            finishOAuthCode(
                                msg.state || expectedState,
                                msg.code,
                                byocAccountId.getValue(),
                                null
                            );
                        } else {
                            finishOAuthCode(
                                msg.state || expectedState,
                                msg.code,
                                relayAccountIdField.getValue(),
                                relayStatusField
                            );
                        }
                    }
                };
                if (typeof global.addEventListener === 'function') {
                    global.addEventListener('message', me._messageHandler);
                }
            };

            me.items = [
                {
                    xtype: 'tabpanel',
                    border: false,
                    defaults: {
                        bodyPadding: 12,
                        layout: 'anchor',
                        defaults: { anchor: '100%', labelWidth: 140 },
                    },
                    items: [
                        {
                            title: _('1-Click Google Sign-In'),
                            items: [
                                relayModeBadge,
                                relayAccountIdField,
                                relayStatusField,
                                {
                                    xtype: 'container',
                                    margin: '6 0 12 0',
                                    items: [
                                        {
                                            xtype: 'button',
                                            text: _('Sign in with Google'),
                                            iconCls: 'fa fa-google',
                                            handler: function () {
                                                relayStatusField.setValue(_('Starting Google OAuth session...'));
                                                var startParams = { mode: 'relay' };
                                                if (uiOrigin) {
                                                    startParams.ui_origin = uiOrigin;
                                                }
                                                Utils.apiRequest({
                                                    url: '/api2/json/gdrive/oauth/start',
                                                    method: 'POST',
                                                    params: startParams,
                                                    waitMsgTarget: me,
                                                    success: function (response) {
                                                        var data = (response.result && response.result.data) || {};
                                                        me._oauthState = data.state || '';
                                                        var authUrl = data.auth_url;
                                                        if (!authUrl) {
                                                            Ext.Msg.alert(_('Error'), _('Daemon did not return an auth_url.'));
                                                            return;
                                                        }

                                                        attachOAuthMessageListener('relay');

                                                        relayStatusField.setValue(_('Waiting for Google sign-in in popup window...'));
                                                        if (typeof global.open === 'function') {
                                                            var popup = global.open(
                                                                authUrl,
                                                                'pve_gdrive_oauth',
                                                                'width=560,height=720,menubar=no,toolbar=no,status=no'
                                                            );
                                                            if (!popup) {
                                                                relayStatusField.setValue(
                                                                    _('Popup blocked! Allow popups or copy the Authorization Payload/Code manually.')
                                                                );
                                                            }
                                                        }
                                                    },
                                                    failure: function (response) {
                                                        relayStatusField.setValue(_('Failed to start OAuth flow.'));
                                                        Ext.Msg.alert(_('Error'), response.htmlStatus || _('Failed to start OAuth flow.'));
                                                    },
                                                });
                                            },
                                        },
                                    ],
                                },
                                relayPayloadField,
                                {
                                    xtype: 'button',
                                    text: _('Complete Sign-In with Pasted Payload'),
                                    anchor: null,
                                    handler: function () {
                                        var rawInput = relayPayloadField.getValue().trim();
                                        if (!rawInput) {
                                            Ext.Msg.alert(_('Invalid Payload'), _('Paste the Authorization Payload or Code first.'));
                                            return;
                                        }
                                        try {
                                            var decoded = Utils.decodeOAuthPayload(rawInput);
                                            var state = decoded.state || me._oauthState;
                                            if (!state) {
                                                Ext.Msg.alert(_('Error'), _('Missing OAuth state. Start sign-in first or paste the full relay payload.'));
                                                return;
                                            }
                                            finishRelayPayload(state, decoded.tokens, relayAccountIdField.getValue());
                                        } catch (err) {
                                            var code = Utils.extractAuthCode(rawInput);
                                            if (code && me._oauthState) {
                                                finishOAuthCode(
                                                    me._oauthState,
                                                    code,
                                                    relayAccountIdField.getValue(),
                                                    relayStatusField
                                                );
                                            } else {
                                                Ext.Msg.alert(_('Invalid Payload'), err.message);
                                            }
                                        }
                                    },
                                },
                            ],
                        },
                        {
                            title: _('Own OAuth App (BYOC)'),
                            items: [
                                byocAccountId,
                                byocClientId,
                                byocClientSecret,
                                byocScope,
                                byocRedirectUri,
                                {
                                    xtype: 'container',
                                    layout: 'hbox',
                                    margin: '4 0 10 0',
                                    items: [
                                        {
                                            xtype: 'button',
                                            text: _('Generate Authorization URL'),
                                            margin: '0 8 0 0',
                                            handler: function () {
                                                if (!byocClientId.isValid() || !byocClientSecret.isValid()) {
                                                    Ext.Msg.alert(_('Error'), _('Client ID and Client Secret are required.'));
                                                    return;
                                                }
                                                var params = {
                                                    mode: 'byoc',
                                                    client_id: byocClientId.getValue().trim(),
                                                    client_secret: byocClientSecret.getValue().trim(),
                                                    scope: byocScope.getValue(),
                                                };
                                                if (uiOrigin) {
                                                    params.ui_origin = uiOrigin;
                                                }
                                                var redir = byocRedirectUri.getValue().trim();
                                                if (redir) {
                                                    params.redirect_uri = redir;
                                                }
                                                Utils.apiRequest({
                                                    url: '/api2/json/gdrive/oauth/start',
                                                    method: 'POST',
                                                    params: params,
                                                    waitMsgTarget: me,
                                                    success: function (response) {
                                                        var data = (response.result && response.result.data) || {};
                                                        me._byocState = data.state || '';
                                                        byocAuthUrlDisplay.setValue(data.auth_url || '');
                                                        byocAuthUrlDisplay.setHidden(false);
                                                        attachOAuthMessageListener('byoc');
                                                        if (data.auth_url && typeof global.open === 'function') {
                                                            global.open(
                                                                data.auth_url,
                                                                'pve_gdrive_byoc_oauth',
                                                                'width=560,height=720,menubar=no,toolbar=no,status=no'
                                                            );
                                                        }
                                                    },
                                                    failure: function (response) {
                                                        Ext.Msg.alert(_('Error'), response.htmlStatus || _('Failed to start BYOC OAuth flow.'));
                                                    },
                                                });
                                            },
                                        },
                                        {
                                            xtype: 'button',
                                            text: _('Use Proxmox UI Callback'),
                                            handler: function () {
                                                if (defaultCallbackUri) {
                                                    byocRedirectUri.setValue(defaultCallbackUri);
                                                }
                                            },
                                        },
                                    ],
                                },
                                byocAuthUrlDisplay,
                                byocCodeField,
                                {
                                    xtype: 'button',
                                    text: _('Complete BYOC Authorization'),
                                    anchor: null,
                                    handler: function () {
                                        var code = Utils.extractAuthCode(byocCodeField.getValue());
                                        if (!me._byocState) {
                                            Ext.Msg.alert(_('Error'), _('Generate an Authorization URL first.'));
                                            return;
                                        }
                                        if (!code) {
                                            Ext.Msg.alert(_('Error'), _('Paste the authorization code or callback URL from Google.'));
                                            return;
                                        }
                                        var params = {
                                            state: me._byocState,
                                            code: code,
                                        };
                                        var byocAcc = byocAccountId.getValue().trim();
                                        if (byocAcc) {
                                            params.account_id = byocAcc;
                                        }
                                        Utils.apiRequest({
                                            url: '/api2/json/gdrive/oauth/finish',
                                            method: 'POST',
                                            params: params,
                                            waitMsgTarget: me,
                                            success: function (response) {
                                                var account =
                                                    (response.result && response.result.data && response.result.data.account) ||
                                                    (response.result && response.result.data) ||
                                                    null;
                                                if (typeof me.onAccountCreated === 'function') {
                                                    me.onAccountCreated(account);
                                                }
                                                me.close();
                                            },
                                            failure: function (response) {
                                                Ext.Msg.alert(_('Error'), response.htmlStatus || _('BYOC authorization failed.'));
                                            },
                                        });
                                    },
                                },
                            ],
                        },
                        {
                            title: _('Service Account JSON'),
                            items: [
                                saAccountId,
                                saJsonField,
                                {
                                    xtype: 'button',
                                    text: _('Add Service Account'),
                                    anchor: null,
                                    handler: function () {
                                        var rawJson = saJsonField.getValue().trim();
                                        if (!rawJson) {
                                            Ext.Msg.alert(_('Error'), _('Service Account JSON is required.'));
                                            return;
                                        }
                                        try {
                                            var parsed = JSON.parse(rawJson);
                                            if (parsed.type !== 'service_account' || !parsed.client_email || !parsed.private_key) {
                                                Ext.Msg.alert(_('Error'), _('JSON must be a valid Google Service Account key.'));
                                                return;
                                            }
                                        } catch (_) {
                                            Ext.Msg.alert(_('Error'), _('Invalid JSON syntax.'));
                                            return;
                                        }

                                        var saParams = {
                                            service_account_json: rawJson,
                                        };
                                        var saAcc = saAccountId.getValue().trim();
                                        if (saAcc) {
                                            saParams.account_id = saAcc;
                                        }
                                        Utils.apiRequest({
                                            url: '/api2/json/gdrive/accounts/service-account',
                                            method: 'POST',
                                            params: saParams,
                                            waitMsgTarget: me,
                                            success: function (response) {
                                                var account =
                                                    (response.result && response.result.data && response.result.data.account) ||
                                                    (response.result && response.result.data) ||
                                                    null;
                                                if (typeof me.onAccountCreated === 'function') {
                                                    me.onAccountCreated(account);
                                                }
                                                me.close();
                                            },
                                    failure: function (response) {
                                        Ext.Msg.alert(_('Error'), response.htmlStatus || _('Failed to register service account.'));
                                    },
                                });
                            },
                        },
                    ],
                },
                {
                    title: _('Host OAuth Relay in Proxmox'),
                    items: [
                        {
                            xtype: 'displayfield',
                            fieldLabel: _('Setup Info'),
                            value: _('Register the Callback Redirect URI below as an Authorized Redirect URI on a Web Application OAuth client in Google Cloud Console. Once enabled, 1-Click Google Sign-In stays 100% inside this Proxmox UI.'),
                        },
                        selfRelayEnabled,
                        selfRelayClientId,
                        selfRelayClientSecret,
                        selfRelayRedirectUri,
                        selfRelayScope,
                        selfRelayStatusDisplay,
                        {
                            xtype: 'container',
                            layout: 'hbox',
                            margin: '8 0 0 0',
                            items: [
                                {
                                    xtype: 'button',
                                    text: _('Save Relay Settings'),
                                    margin: '0 8 0 0',
                                    handler: function () {
                                        var enabled = Boolean(selfRelayEnabled.getValue());
                                        var cid = selfRelayClientId.getValue().trim();
                                        if (enabled && !cid) {
                                            Ext.Msg.alert(_('Error'), _('Google Client ID is required when enabling the Proxmox-hosted relay.'));
                                            return;
                                        }
                                        var saveParams = {
                                            enabled: enabled ? 1 : 0,
                                            scope: selfRelayScope.getValue(),
                                        };
                                        if (cid) {
                                            saveParams.client_id = cid;
                                        }
                                        var csec = selfRelayClientSecret.getValue().trim();
                                        if (csec) {
                                            saveParams.client_secret = csec;
                                        }
                                        var redir = selfRelayRedirectUri.getValue().trim();
                                        if (redir) {
                                            saveParams.redirect_uri = redir;
                                        }
                                        Utils.apiRequest({
                                            url: '/api2/json/gdrive/relay/config',
                                            method: 'POST',
                                            params: saveParams,
                                            waitMsgTarget: me,
                                            success: function (response) {
                                                var saved = (response.result && response.result.data) || {};
                                                selfRelayClientSecret.setValue('');
                                                updateRelayStatusUI(saved);
                                                Ext.Msg.alert(
                                                    _('Saved'),
                                                    enabled
                                                        ? _('Self-hosted Proxmox OAuth relay enabled. You can now use 1-Click Google Sign-In directly.')
                                                        : _('Self-hosted Proxmox OAuth relay disabled.')
                                                );
                                            },
                                            failure: function (response) {
                                                Ext.Msg.alert(_('Error'), response.htmlStatus || _('Failed to save relay settings.'));
                                            },
                                        });
                                    },
                                },
                                {
                                    xtype: 'button',
                                    text: _('Copy Callback URI'),
                                    handler: function () {
                                        var uri = selfRelayRedirectUri.getValue().trim() || defaultCallbackUri;
                                        if (uri && global.navigator && global.navigator.clipboard && typeof global.navigator.clipboard.writeText === 'function') {
                                            global.navigator.clipboard.writeText(uri);
                                        }
                                    },
                                },
                            ],
                        },
                    ],
                },
            ],
        },
    ];

            me.buttons = [
                {
                    text: _('Close'),
                    handler: function () {
                        me.close();
                    },
                },
            ];

            me.callParent();

            Utils.apiRequest({
                url: '/api2/json/gdrive/relay/config',
                method: 'GET',
                success: function (response) {
                    updateRelayStatusUI((response.result && response.result.data) || {});
                },
                failure: function () {
                    updateRelayStatusUI({ enabled: false });
                },
            });
        },

        _detachMessageListener: function () {
            var me = this;
            if (me._messageHandler && typeof global.removeEventListener === 'function') {
                global.removeEventListener('message', me._messageHandler);
                me._messageHandler = null;
            }
        },

        onDestroy: function () {
            this._detachMessageListener();
            this.callParent();
        },
    });

    /**
     * Google Drive Storage Add/Edit Input Panel.
     */
    Ext.define('PVE.storage.GDriveInputPanel', {
        extend: 'PVE.panel.StorageBase',
        alias: 'widget.pveGDriveInputPanel',

        onlineHelp: 'chapter_storage',

        initComponent: function () {
            var me = this;

            var accountStore = Ext.create('Ext.data.Store', {
                fields: ['id', 'email', 'mode', 'scope', 'label'],
                data: [],
            });

            var driveStore = Ext.create('Ext.data.Store', {
                fields: ['id', 'name'],
                data: [{ id: '', name: _('My Drive') }],
            });

            var loadDrives = function (accountId) {
                if (!accountId) {
                    driveStore.setData([{ id: '', name: _('My Drive') }]);
                    return;
                }
                Utils.apiRequest({
                    url: '/api2/json/gdrive/accounts/' + encodeURIComponent(accountId) + '/drives',
                    method: 'GET',
                    success: function (response) {
                        var drives =
                            (response.result && response.result.data && response.result.data.drives) || [];
                        var items = [{ id: '', name: _('My Drive') }];
                        for (var i = 0; i < drives.length; i += 1) {
                            if (drives[i] && drives[i].id) {
                                items.push({
                                    id: drives[i].id,
                                    name: drives[i].name || drives[i].id,
                                });
                            }
                        }
                        driveStore.setData(items);
                    },
                    failure: function () {
                        driveStore.setData([{ id: '', name: _('My Drive') }]);
                    },
                });
            };

            var accountField = Ext.create('Ext.form.field.ComboBox', {
                name: 'account',
                fieldLabel: _('Google Account'),
                store: accountStore,
                valueField: 'id',
                displayField: 'label',
                queryMode: 'local',
                editable: false,
                forceSelection: false,
                allowBlank: false,
                emptyText: _('Select or sign in with Google...'),
                listeners: {
                    change: function (_field, newVal) {
                        loadDrives(newVal);
                    },
                },
            });

            var loadAccounts = function (selectAccountId) {
                Utils.apiRequest({
                    url: '/api2/json/gdrive/accounts',
                    method: 'GET',
                    success: function (response) {
                        var accounts =
                            (response.result && response.result.data && response.result.data.accounts) || [];
                        var rows = [];
                        for (var i = 0; i < accounts.length; i += 1) {
                            var acc = accounts[i];
                            rows.push({
                                id: acc.id,
                                email: acc.email || acc.id,
                                mode: acc.mode || 'oauth',
                                scope: acc.scope || '',
                                label:
                                    acc.id +
                                    (acc.email ? ' (' + acc.email + ')' : '') +
                                    (acc.scope ? ' [' + acc.scope + ']' : ''),
                            });
                        }
                        accountStore.setData(rows);
                        if (selectAccountId) {
                            accountField.setValue(selectAccountId);
                        }
                    },
                });
            };

            var driveField = Ext.create('Ext.form.field.ComboBox', {
                name: 'drive_id',
                fieldLabel: _('Shared Drive'),
                store: driveStore,
                valueField: 'id',
                displayField: 'name',
                queryMode: 'local',
                editable: false,
                value: '',
            });

            var passphraseField = Ext.create('Ext.form.field.Text', {
                name: 'crypt_passphrase',
                fieldLabel: _('Encryption Passphrase'),
                inputType: 'password',
                hidden: true,
                disabled: true,
                allowBlank: !me.isCreate,
                emptyText: me.isCreate
                    ? _('Enter strong passphrase')
                    : _('Leave blank to keep existing passphrase'),
            });

            var passphraseConfirmField = Ext.create('Ext.form.field.Text', {
                name: 'crypt_passphrase_confirm',
                fieldLabel: _('Confirm Passphrase'),
                inputType: 'password',
                hidden: true,
                disabled: true,
                submitValue: false,
                validator: function (val) {
                    var pass = passphraseField.getValue();
                    if (!pass && !me.isCreate) {
                        return true;
                    }
                    return val === pass ? true : _('Passphrases do not match.');
                },
            });

            var cryptWarningField = Ext.create('Ext.form.field.Display', {
                hidden: true,
                value:
                    '<span style="color:#b45309;font-weight:600;">' +
                    _('Warning: Client-side encryption (rclone crypt) encrypts files and filenames before upload. Back up your passphrase offline — lost passphrases cannot be recovered!') +
                    '</span>',
            });

            var toggleCryptFields = function (enabled) {
                var show = Boolean(enabled);
                passphraseField.setHidden(!show);
                passphraseField.setDisabled(!show);
                passphraseConfirmField.setHidden(!show);
                passphraseConfirmField.setDisabled(!show);
                cryptWarningField.setHidden(!show);
            };

            me.column1 = [
                accountField,
                {
                    xtype: 'container',
                    margin: '0 0 8 0',
                    items: [
                        {
                            xtype: 'button',
                            text: _('Sign in with Google'),
                            iconCls: 'fa fa-google',
                            handler: function () {
                                Ext.create('PVE.gdrive.AuthWizardWindow', {
                                    onAccountCreated: function (account) {
                                        var createdId = account && account.id ? account.id : null;
                                        loadAccounts(createdId);
                                    },
                                }).show();
                            },
                        },
                    ],
                },
                driveField,
                {
                    xtype: 'proxmoxtextfield',
                    name: 'subfolder',
                    fieldLabel: _('Subfolder'),
                    emptyText: _('Optional subfolder inside proxmox/'),
                    deleteEmpty: !me.isCreate,
                },
            ];

            me.column2 = [
                {
                    xtype: 'pveContentTypeSelector',
                    cts: ['backup', 'iso', 'vztmpl'],
                    fieldLabel: _('Content'),
                    name: 'content',
                    value: 'backup,iso,vztmpl',
                    multiSelect: true,
                    allowBlank: false,
                },
                {
                    xtype: 'proxmoxcheckbox',
                    name: 'wait_for_upload',
                    fieldLabel: _('Backup Sync'),
                    boxLabel: _('Wait for Google Drive upload on backup'),
                    checked: true,
                    uncheckedValue: false,
                },
                {
                    xtype: 'proxmoxtextfield',
                    name: 'cache_dir',
                    fieldLabel: _('Cache Directory'),
                    value: '/var/cache/pve-gdrive',
                    emptyText: '/var/cache/pve-gdrive',
                    deleteEmpty: !me.isCreate,
                },
                {
                    xtype: 'proxmoxtextfield',
                    name: 'cache_max_size',
                    fieldLabel: _('Max Cache Size'),
                    emptyText: _('Auto (min 20G, 25% free)'),
                    deleteEmpty: !me.isCreate,
                },
            ];

            me.columnB = [
                {
                    xtype: 'proxmoxcheckbox',
                    name: 'encrypt',
                    fieldLabel: _('Encryption'),
                    boxLabel: _('Client-Side Encryption (rclone crypt)'),
                    checked: false,
                    uncheckedValue: 0,
                    inputValue: 1,
                    listeners: {
                        change: function (_cb, newVal) {
                            toggleCryptFields(newVal);
                        },
                    },
                },
                passphraseField,
                passphraseConfirmField,
                cryptWarningField,
            ];

            me._loadAccounts = loadAccounts;
            me._loadDrives = loadDrives;
            me._toggleCryptFields = toggleCryptFields;

            me.callParent();
            loadAccounts();
        },

        setValues: function (values) {
            var me = this;
            var copy = Ext.apply({}, values || {});
            // Checked by default (async_upload = 0); unchecked only when async_upload is truthy/1.
            var isAsync =
                copy.async_upload === 1 ||
                copy.async_upload === '1' ||
                copy.async_upload === true;
            copy.wait_for_upload = !isAsync;
            if (copy.account && typeof me._loadDrives === 'function') {
                me._loadDrives(copy.account);
            }
            if (typeof me._toggleCryptFields === 'function') {
                me._toggleCryptFields(
                    copy.encrypt === 1 || copy.encrypt === '1' || copy.encrypt === true
                );
            }
            return me.callParent([copy]);
        },

        onGetValues: function (values) {
            var me = this;
            var out = Ext.apply({}, values || {});
            out.async_upload = out.wait_for_upload === false ? 1 : 0;
            delete out.wait_for_upload;
            delete out.crypt_passphrase_confirm;

            out.encrypt =
                out.encrypt === 1 || out.encrypt === '1' || out.encrypt === true ? 1 : 0;
            if (!out.encrypt || (!me.isCreate && !out.crypt_passphrase)) {
                delete out.crypt_passphrase;
            }
            if (!out.drive_id) {
                delete out.drive_id;
            }

            return me.callParent([out]);
        },
    });

    /**
     * Modal to import ISO / LXC template files via Google Picker or File IDs
     * for `drive.file` scoped accounts.
     */
    Ext.define('PVE.gdrive.PickerModal', {
        extend: 'Ext.window.Window',
        alias: 'widget.pveGDrivePickerModal',

        title: _('Import from Google Drive'),
        modal: true,
        width: 540,
        bodyPadding: 12,
        layout: 'anchor',
        defaults: { anchor: '100%', labelWidth: 130 },

        storeid: null,
        account: null,
        targetType: 'iso',
        onImported: null,

        initComponent: function () {
            var me = this;

            var targetTypeCombo = Ext.create('Ext.form.field.ComboBox', {
                fieldLabel: _('Target Type'),
                store: [
                    ['iso', _('ISO image (iso)')],
                    ['vztmpl', _('Container template (vztmpl)')],
                ],
                value: me.targetType === 'vztmpl' ? 'vztmpl' : 'iso',
                editable: false,
                forceSelection: true,
                queryMode: 'local',
            });

            var accessTokenField = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Picker Access Token'),
                inputType: 'password',
                emptyText: _('Optional short-lived Google Picker OAuth token'),
            });

            var fileIdsField = Ext.create('Ext.form.field.TextArea', {
                fieldLabel: _('Drive File IDs / URLs'),
                height: 110,
                allowBlank: false,
                emptyText: _('Enter one or more Google Drive File IDs or share URLs, separated by commas or newlines.'),
            });

            me.items = [
                {
                    xtype: 'displayfield',
                    value: _(
                        'Select files with Google Picker or paste Google Drive file IDs/URLs to copy into this storage.'
                    ),
                },
                targetTypeCombo,
                accessTokenField,
                fileIdsField,
                {
                    xtype: 'button',
                    text: _('Open Google Picker'),
                    anchor: null,
                    margin: '4 0 8 0',
                    handler: function () {
                        var token = accessTokenField.getValue().trim();
                        if (
                            global.gapi &&
                            global.google &&
                            global.google.picker &&
                            token
                        ) {
                            var view = new global.google.picker.DocsView();
                            var picker = new global.google.picker.PickerBuilder()
                                .addView(view)
                                .setOAuthToken(token)
                                .setCallback(function (data) {
                                    if (
                                        data &&
                                        data.action === global.google.picker.Action.PICKED &&
                                        Array.isArray(data.docs)
                                    ) {
                                        var picked = data.docs.map(function (d) { return d.id; });
                                        fileIdsField.setValue(picked.join('\n'));
                                    }
                                })
                                .build();
                            picker.setVisible(true);
                        } else {
                            Ext.Msg.alert(
                                _('Google Picker'),
                                _('Google Picker JS API requires a Picker Access Token and online access to apis.google.com. You can also paste Drive File IDs or URLs directly below.')
                            );
                        }
                    },
                },
            ];

            me.buttons = [
                {
                    text: _('Import'),
                    handler: function () {
                        var fileIds = Utils.extractDriveFileIds(fileIdsField.getValue());
                        if (!fileIds.length) {
                            Ext.Msg.alert(_('Error'), _('Enter at least one Google Drive file ID or URL.'));
                            return;
                        }
                        var importParams = {
                            storeid: me.storeid,
                            file_ids: JSON.stringify(fileIds),
                            target_type: targetTypeCombo.getValue(),
                        };
                        if (me.account) {
                            importParams.account = me.account;
                        }
                        var pickerToken = accessTokenField.getValue().trim();
                        if (pickerToken) {
                            importParams.access_token = pickerToken;
                        }
                        Utils.apiRequest({
                            url: '/api2/json/gdrive/picker/import',
                            method: 'POST',
                            params: importParams,
                            waitMsgTarget: me,
                            success: function () {
                                if (typeof me.onImported === 'function') {
                                    me.onImported();
                                }
                                me.close();
                            },
                            failure: function (response) {
                                Ext.Msg.alert(_('Import Failed'), response.htmlStatus || _('Unable to import files from Google Drive.'));
                            },
                        });
                    },
                },
                {
                    text: _('Cancel'),
                    handler: function () {
                        me.close();
                    },
                },
            ];

            me.callParent();
        },
    });

    /**
     * Enhance PVE.storage.ContentView toolbar with:
     *  - Live Drive Upload Queue / Cache status indicator
     *  - "Import from Google Drive" button on iso/vztmpl views for drive.file accounts
     */
    PVE.gdrive.installContentViewHooks = function () {
        if (!PVE.storage || !PVE.storage.ContentView || PVE.gdrive._contentHooksInstalled) {
            return;
        }
        PVE.gdrive._contentHooksInstalled = true;

        Ext.override(PVE.storage.ContentView, {
            initComponent: function () {
                var me = this;
                me.callParent(arguments);

                if (!Utils.isGDriveStorage(me)) {
                    return;
                }

                var queueIndicator = Ext.create('Ext.toolbar.TextItem', {
                    text: _('Drive: checking...'),
                });

                var importBtn = Ext.create('Ext.button.Button', {
                    text: _('Import from Google Drive'),
                    iconCls: 'fa fa-cloud-download',
                    hidden: true,
                    handler: function () {
                        Ext.create('PVE.gdrive.PickerModal', {
                            storeid: me.storage,
                            account: me._gdriveAccount || null,
                            targetType: me.content === 'vztmpl' ? 'vztmpl' : 'iso',
                            onImported: function () {
                                if (me.store && typeof me.store.reload === 'function') {
                                    me.store.reload();
                                }
                            },
                        }).show();
                    },
                });

                var refreshQueueStatus = function () {
                    Utils.apiRequest({
                        url: '/api2/json/gdrive/queue',
                        method: 'GET',
                        success: function (response) {
                            var data = (response.result && response.result.data) || {};
                            var uploads = Array.isArray(data.uploads) ? data.uploads : [];
                            var stInfo = (data.storages && data.storages[me.storage]) || {};
                            var storeUploads = uploads.filter(function (u) {
                                return !u.storeid || u.storeid === me.storage;
                            });
                            var queueText = storeUploads.length
                                ? _('Uploading ') + storeUploads.length + _(' file(s)')
                                : _('Idle');
                            var cacheText = '';
                            if (stInfo.cache_used !== undefined) {
                                cacheText =
                                    ' | ' +
                                    _('Cache: ') +
                                    Utils.formatBytes(stInfo.cache_used) +
                                    (stInfo.cache_max
                                        ? ' / ' + Utils.formatBytes(stInfo.cache_max)
                                        : '');
                            }
                            queueIndicator.setText(_('Drive: ') + queueText + cacheText);
                        },
                        failure: function () {
                            queueIndicator.setText(_('Drive: status unavailable'));
                        },
                    });
                };

                var checkPickerEligibility = function () {
                    var isMediaView =
                        !me.content || me.content === 'iso' || me.content === 'vztmpl';
                    if (!isMediaView) {
                        importBtn.setHidden(true);
                        return;
                    }
                    Utils.apiRequest({
                        url: '/api2/json/gdrive/accounts',
                        method: 'GET',
                        success: function (response) {
                            var accounts =
                                (response.result && response.result.data && response.result.data.accounts) || [];
                            var storageAccountId =
                                me.storageInfo && me.storageInfo.account
                                    ? me.storageInfo.account
                                    : null;
                            var matched = null;
                            for (var i = 0; i < accounts.length; i += 1) {
                                if (!storageAccountId || accounts[i].id === storageAccountId) {
                                    matched = accounts[i];
                                    break;
                                }
                            }
                            if (matched) {
                                me._gdriveAccount = matched.id;
                                var scopeStr = String(matched.scope || '');
                                var isDriveFile =
                                    scopeStr.indexOf('drive.file') !== -1 || scopeStr === '';
                                importBtn.setHidden(!isDriveFile);
                            }
                        },
                    });
                };

                var toolbar =
                    typeof me.getDockedItems === 'function'
                        ? me.getDockedItems('toolbar[dock="top"]')[0]
                        : null;
                if (toolbar && typeof toolbar.add === 'function') {
                    toolbar.add(importBtn, '->', queueIndicator);
                }

                refreshQueueStatus();
                checkPickerEligibility();

                me._gdriveQueueTimer = global.setInterval(refreshQueueStatus, 10000);
                me.on('destroy', function () {
                    if (me._gdriveQueueTimer) {
                        global.clearInterval(me._gdriveQueueTimer);
                        me._gdriveQueueTimer = null;
                    }
                });
            },
        });
    };

    PVE.gdrive.installContentViewHooks();
})(typeof window !== 'undefined' ? window : globalThis);
