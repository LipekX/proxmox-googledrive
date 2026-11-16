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
                var reqOpts = Ext && typeof Ext.apply === 'function' ? Ext.apply({}, opts) : Object.assign({}, opts);
                if (typeof reqOpts.url === 'string') {
                    reqOpts.url = reqOpts.url.replace(/^\/api2\/json\//, '/api2/extjs/');
                }
                return Proxmox.Utils.API2Request(reqOpts);
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

    var runWhenReady =
        typeof Ext.onReady === 'function'
            ? function (fn) { Ext.onReady(fn); }
            : function (fn) { fn(); };

    /**
     * One-Time Google Sign-In Modal (supports browser OAuth link + redirect URL paste,
     * `rclone authorize "drive"` token paste, or Service Account JSON).
     */
    Ext.define('PVE.gdrive.AuthWizardWindow', {
        extend: 'Ext.window.Window',
        alias: 'widget.pveGDriveAuthWizard',

        title: _('Sign in with Google'),
        modal: true,
        width: 580,
        bodyPadding: 12,
        resizable: false,

        onAccountCreated: null,

        initComponent: function () {
            var me = this;
            var currentOauthState = '';

            var accountIdField = Ext.create('Ext.form.field.Text', {
                fieldLabel: _('Profile ID (optional)'),
                emptyText: _('Auto-derived from your Google email if blank'),
                width: '100%',
            });

            var inputArea = Ext.create('Ext.form.field.TextArea', {
                fieldLabel: _('Redirect URL / Code'),
                emptyText: _('Paste the http://127.0.0.1:53682/?code=... URL from your browser address bar (or rclone token / Service Account JSON)...'),
                height: 150,
                width: '100%',
                allowBlank: false,
            });

            var fileField = Ext.create('Ext.form.field.File', {
                fieldLabel: _('Or Upload JSON Key'),
                buttonText: _('Browse...'),
                width: '100%',
                listeners: {
                    change: function (field) {
                        var fileInput =
                            field.fileInputEl && field.fileInputEl.dom
                                ? field.fileInputEl.dom
                                : null;
                        var file =
                            fileInput && fileInput.files && fileInput.files[0]
                                ? fileInput.files[0]
                                : null;
                        if (file && typeof global.FileReader === 'function') {
                            var reader = new global.FileReader();
                            reader.onload = function (ev) {
                                if (ev && ev.target && typeof ev.target.result === 'string') {
                                    inputArea.setValue(ev.target.result);
                                }
                            };
                            reader.readAsText(file);
                        }
                    },
                },
            });

            var openGoogleSignIn = function () {
                Utils.apiRequest({
                    url: '/api2/json/gdrive/oauth/start',
                    method: 'POST',
                    params: { scope: 'drive' },
                    waitMsgTarget: me,
                    success: function (response) {
                        var data = (response.result && response.result.data) || {};
                        currentOauthState = data.state || '';
                        if (data.auth_url && typeof global.open === 'function') {
                            global.open(data.auth_url, '_blank', 'noopener,noreferrer');
                        } else if (data.auth_url) {
                            Ext.Msg.alert(_('Google Sign-In URL'), data.auth_url);
                        }
                    },
                    failure: function (response) {
                        Ext.Msg.alert(
                            _('Google Sign-In Error'),
                            response.htmlStatus || _('Failed to start Google OAuth flow.')
                        );
                    },
                });
            };

            me.items = [
                {
                    xtype: 'displayfield',
                    value:
                        '<b>' +
                        _('Option A — One-Click Browser Login:') +
                        '</b><br>' +
                        _('1) Click "1. Open Google Sign-In" below and log in with your regular Google account.') +
                        '<br>' +
                        _('2) When Google redirects to <code>http://127.0.0.1:53682/?code=...</code> (even if the page says Unable to connect), copy the full URL from your browser address bar and paste it below.') +
                        '<br><br><b>' +
                        _('Option B — CLI Login:') +
                        '</b> ' +
                        _('Run <code>pve-gdrive login</code> on your Proxmox node via SSH.'),
                },
                {
                    xtype: 'button',
                    text: _('1. Open Google Sign-In in Browser'),
                    iconCls: 'fa fa-external-link',
                    margin: '4 0 10 0',
                    handler: openGoogleSignIn,
                },
                accountIdField,
                inputArea,
                fileField,
            ];

            me.buttons = [
                {
                    text: _('Save Google Account'),
                    iconCls: 'fa fa-check',
                    handler: function () {
                        var rawText = inputArea.getValue().trim();
                        if (!rawText) {
                            Ext.Msg.alert(_('Error'), _('Paste the redirected Google URL, authorization code, or JSON key.'));
                            return;
                        }
                        var acctIdVal = accountIdField.getValue().trim();
                        var isServiceAccount =
                            rawText.charAt(0) === '{' &&
                            rawText.indexOf('"service_account"') !== -1 &&
                            rawText.indexOf('"private_key"') !== -1;

                        if (isServiceAccount) {
                            var saParams = { service_account_json: rawText };
                            if (acctIdVal) {
                                saParams.account_id = acctIdVal;
                            }
                            Utils.apiRequest({
                                url: '/api2/json/gdrive/accounts/service-account',
                                method: 'POST',
                                params: saParams,
                                waitMsgTarget: me,
                                success: function (response) {
                                    var account =
                                        response.result &&
                                        response.result.data &&
                                        response.result.data.account;
                                    if (typeof me.onAccountCreated === 'function') {
                                        me.onAccountCreated(account);
                                    }
                                    me.close();
                                },
                                failure: function (response) {
                                    Ext.Msg.alert(
                                        _('Account Error'),
                                        response.htmlStatus || _('Failed to save Google Account.')
                                    );
                                },
                            });
                            return;
                        }

                        var oauthParams = { code: rawText };
                        if (currentOauthState) {
                            oauthParams.state = currentOauthState;
                        }
                        if (acctIdVal) {
                            oauthParams.account_id = acctIdVal;
                        }
                        Utils.apiRequest({
                            url: '/api2/json/gdrive/oauth/finish',
                            method: 'POST',
                            params: oauthParams,
                            waitMsgTarget: me,
                            success: function (response) {
                                var account =
                                    response.result &&
                                    response.result.data &&
                                    response.result.data.account;
                                if (typeof me.onAccountCreated === 'function') {
                                    me.onAccountCreated(account);
                                }
                                me.close();
                            },
                            failure: function (response) {
                                Ext.Msg.alert(
                                    _('Google Sign-In Error'),
                                    response.htmlStatus || _('Failed to complete Google Sign-In.')
                                );
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

            if (me.superclass) {
                if (typeof me.superclass.initComponent === 'function') {
                    me.superclass.initComponent.call(me);
                }
            } else if (typeof me.callParent === 'function') {
                me.callParent();
            }
        },
    });

    /**
     * Google Drive Storage Add/Edit Input Panel.
     */
    runWhenReady(function () {
        var dirSuper =
            PVE.storage &&
            PVE.storage.DirInputPanel &&
            PVE.storage.DirInputPanel.superclass &&
            PVE.storage.DirInputPanel.superclass.$className;
        var baseInputClass =
            dirSuper ||
            (Proxmox && Proxmox.panel && Proxmox.panel.InputPanel
                ? 'Proxmox.panel.InputPanel'
                : 'PVE.panel.StorageBase');

        Ext.define('PVE.storage.GDriveInputPanel', {
            extend: baseInputClass,
            alias: 'widget.pveGDriveInputPanel',

        onlineHelp: 'chapter_storage',

        initComponent: function () {
            var me = this;

            var accountStore = Ext.create('Ext.data.Store', {
                fields: ['id', 'email', 'mode', 'scope', 'label'],
                data: [],
            });

            var MY_DRIVE_VAL = '__my_drive__';
            var driveStore = Ext.create('Ext.data.Store', {
                fields: ['drive_id', 'name'],
                data: [{ drive_id: MY_DRIVE_VAL, name: _('My Drive') }],
            });

            var loadDrives = function (accountId) {
                if (!accountId) {
                    driveStore.setData([{ drive_id: MY_DRIVE_VAL, name: _('My Drive') }]);
                    return;
                }
                Utils.apiRequest({
                    url: '/api2/json/gdrive/accounts/' + encodeURIComponent(accountId) + '/drives',
                    method: 'GET',
                    success: function (response) {
                        var drives =
                            (response.result && response.result.data && response.result.data.drives) || [];
                        var items = [{ drive_id: MY_DRIVE_VAL, name: _('My Drive') }];
                        for (var i = 0; i < drives.length; i += 1) {
                            if (drives[i] && drives[i].id) {
                                items.push({
                                    drive_id: drives[i].id,
                                    name: drives[i].name || drives[i].id,
                                });
                            }
                        }
                        driveStore.setData(items);
                    },
                    failure: function () {
                        driveStore.setData([{ drive_id: MY_DRIVE_VAL, name: _('My Drive') }]);
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
                emptyText: _('Select or sign in to a Google Account...'),
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
                                mode: acc.mode || 'service_account',
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
                valueField: 'drive_id',
                displayField: 'name',
                queryMode: 'local',
                editable: false,
                value: MY_DRIVE_VAL,
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
                {
                    xtype: 'proxmoxtextfield',
                    name: 'write_bwlimit',
                    fieldLabel: _('Max Write Speed'),
                    emptyText: _('Unlimited (e.g. 20M, 50M)'),
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

            if (me.superclass) {
                if (typeof me.superclass.initComponent === 'function') {
                    me.superclass.initComponent.call(me);
                }
            } else if (typeof me.callParent === 'function') {
                me.callParent();
            }
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
            if (!copy.drive_id) {
                copy.drive_id = '__my_drive__';
            }
            if (copy.account && typeof me._loadDrives === 'function') {
                me._loadDrives(copy.account);
            }
            if (typeof me._toggleCryptFields === 'function') {
                me._toggleCryptFields(
                    copy.encrypt === 1 || copy.encrypt === '1' || copy.encrypt === true
                );
            }
            if (me.superclass) {
                if (typeof me.superclass.setValues === 'function') {
                    return me.superclass.setValues.call(me, copy);
                }
                return copy;
            }
            return typeof me.callParent === 'function' ? me.callParent([copy]) : copy;
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
            if (!out.drive_id || out.drive_id === '__my_drive__') {
                delete out.drive_id;
            }
            if (!out.write_bwlimit && me.isCreate) {
                delete out.write_bwlimit;
            }

            if (me.superclass) {
                if (typeof me.superclass.onGetValues === 'function') {
                    return me.superclass.onGetValues.call(me, out);
                }
                return out;
            }
            return typeof me.callParent === 'function' ? me.callParent([out]) : out;
        },
    });
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

            if (me.superclass) {
                if (typeof me.superclass.initComponent === 'function') {
                    me.superclass.initComponent.call(me);
                }
            } else if (typeof me.callParent === 'function') {
                me.callParent();
            }
        },
    });

    /**
     * Enhance PVE.storage.ContentView toolbar with:
     *  - Live Drive Upload Queue / Cache status indicator
     *  - "Import from Google Drive" button on iso/vztmpl views for drive.file accounts
     */
    PVE.gdrive.installContentViewHooks = function () {
        if (
            !PVE.storage ||
            !PVE.storage.ContentView ||
            !PVE.storage.ContentView.prototype ||
            PVE.gdrive._contentHooksInstalled
        ) {
            return;
        }
        var origInitComponent = PVE.storage.ContentView.prototype.initComponent;
        if (typeof origInitComponent !== 'function') {
            return;
        }
        PVE.gdrive._contentHooksInstalled = true;

        PVE.storage.ContentView.prototype.initComponent = function () {
            var me = this;
            origInitComponent.apply(me, arguments);

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
                var applyAccounts = function (accounts) {
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
                };
                var now = Date.now();
                if (
                    PVE.gdrive._accountsCache &&
                    now - PVE.gdrive._accountsCache.ts < 60000
                ) {
                    applyAccounts(PVE.gdrive._accountsCache.accounts);
                    return;
                }
                Utils.apiRequest({
                    url: '/api2/json/gdrive/accounts',
                    method: 'GET',
                    success: function (response) {
                        var accounts =
                            (response.result && response.result.data && response.result.data.accounts) || [];
                        PVE.gdrive._accountsCache = { ts: Date.now(), accounts: accounts };
                        applyAccounts(accounts);
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

            me._gdriveQueueTimer = global.setInterval(function () {
                if (typeof me.isVisible === 'function' && !me.isVisible(true)) {
                    return;
                }
                refreshQueueStatus();
            }, 10000);
            me.on('destroy', function () {
                if (me._gdriveQueueTimer) {
                    global.clearInterval(me._gdriveQueueTimer);
                    me._gdriveQueueTimer = null;
                }
            });
        };
    };

    runWhenReady(function () {
        try {
            PVE.gdrive.installContentViewHooks();
        } catch (err) {
            if (global.console && typeof global.console.error === 'function') {
                global.console.error('pve-gdrive UI hook warning:', err);
            }
        }
    });
})(typeof window !== 'undefined' ? window : globalThis);
