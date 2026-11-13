package PVE::API2::GDrive;

use strict;
use warnings;

use parent qw(PVE::RESTHandler);

use IO::Socket::UNIX qw(SOCK_STREAM);
use IO::Select;
use JSON qw(encode_json decode_json);
use PVE::JSONSchema qw(get_standard_option);

use constant {
    DEFAULT_SOCK_PATH => '/run/pve-gdrived.sock',
    IPC_TIMEOUT_SEC   => 30,
};

sub _sock_path {
    return $ENV{PVE_GDRIVE_SOCK} || DEFAULT_SOCK_PATH;
}

sub _url_encode {
    my ($str) = @_;
    $str //= '';
    $str =~ s/([^A-Za-z0-9_.~-])/sprintf('%%%02X', ord($1))/ge;
    return $str;
}

sub _decode_chunked {
    my ($in) = @_;
    my $out = '';
    my $pos = 0;
    while ($pos < length($in)) {
        my $crlf = index($in, "\r\n", $pos);
        last if $crlf < 0;
        my $hex = substr($in, $pos, $crlf - $pos);
        $hex =~ s/;.*$//;
        last unless $hex =~ /^[0-9a-fA-F]+$/;
        my $len = hex($hex);
        $pos = $crlf + 2;
        last if $len == 0;
        $out .= substr($in, $pos, $len);
        $pos += $len + 2;
    }
    return $out;
}

sub _ipc_request {
    my ($method, $path, $payload, $timeout) = @_;
    $timeout //= IPC_TIMEOUT_SEC;

    my $sock_path = _sock_path();
    my $sock = IO::Socket::UNIX->new(
        Type    => SOCK_STREAM,
        Peer    => $sock_path,
        Timeout => $timeout,
    ) or die "cannot connect to pve-gdrived socket '$sock_path': $!\n";

    my $body = defined($payload) ? encode_json($payload) : '';
    my $body_len = do { use bytes; length($body) };

    my $req = "$method $path HTTP/1.1\r\n"
        . "Host: localhost\r\n"
        . "Connection: close\r\n"
        . "Accept: application/json\r\n";
    if (defined $payload) {
        $req .= "Content-Type: application/json\r\n"
            . "Content-Length: $body_len\r\n";
    }
    $req .= "\r\n" . $body;

    my $sel = IO::Select->new($sock);
    my $written = 0;
    my $req_len = do { use bytes; length($req) };
    while ($written < $req_len) {
        die "timed out writing to pve-gdrived\n" unless $sel->can_write($timeout);
        my $n = syswrite($sock, $req, $req_len - $written, $written);
        die "write error to pve-gdrived: $!\n" unless defined $n;
        $written += $n;
    }

    my $now = sub { eval { require Time::HiRes; Time::HiRes::time() } // time() };
    my $raw = '';
    my $deadline = $now->() + $timeout;
    while (1) {
        my $rem = $deadline - $now->();
        if ($rem <= 0) {
            die "timed out reading response from pve-gdrived\n" if $raw eq '';
            last;
        }
        die "timed out reading response from pve-gdrived\n" unless $sel->can_read($rem);
        my $buf = '';
        my $n = sysread($sock, $buf, 65536);
        die "read error from pve-gdrived: $!\n" unless defined $n;
        last if $n == 0;
        $raw .= $buf;
        if ($raw =~ /\r\n\r\n/s) {
            my ($hdrs, $rest) = split(/\r\n\r\n/, $raw, 2);
            if ($hdrs =~ /^Content-Length:\s*(\d+)/mi) {
                my $cl = int($1);
                last if do { use bytes; length($rest) } >= $cl;
            }
        }
    }
    close($sock);

    my ($hdr_block, $resp_body) = split(/\r\n\r\n/, $raw, 2);
    die "malformed HTTP response from pve-gdrived\n" unless defined $hdr_block;

    my ($status_line) = split(/\r\n/, $hdr_block, 2);
    die "missing HTTP status line from pve-gdrived\n"
        unless defined($status_line) && $status_line =~ m!^HTTP/1\.\d\s+(\d{3})(?:\s+(.*))?$!;
    my ($status_code, $status_reason) = (int($1), $2 // '');

    $resp_body //= '';
    if ($hdr_block =~ /^Transfer-Encoding:\s*chunked\b/mi) {
        $resp_body = _decode_chunked($resp_body);
    }

    my $data;
    if ($resp_body =~ /\S/) {
        $data = eval { decode_json($resp_body) };
        if ($@) {
            die "invalid JSON from pve-gdrived (HTTP $status_code): $@\n" if $status_code >= 400;
            $data = { raw => $resp_body };
        }
    }

    if ($status_code >= 400) {
        my $msg = ($data && ref($data) eq 'HASH')
            ? ($data->{error} // $data->{message} // $status_reason)
            : $status_reason;
        die "pve-gdrived error (HTTP $status_code): $msg\n";
    }

    return $data // {};
}

sub _parse_file_ids {
    my ($raw) = @_;
    return $raw if ref($raw) eq 'ARRAY';
    die "file_ids must not be empty\n" unless defined $raw && $raw =~ /\S/;
    my $trimmed = $raw;
    $trimmed =~ s/^\s+|\s+$//g;
    if ($trimmed =~ /^\[/) {
        my $decoded = eval { decode_json($trimmed) };
        if (!$@ && ref($decoded) eq 'ARRAY') {
            return [ grep { defined($_) && $_ ne '' } @$decoded ];
        }
    }
    my @ids = grep { $_ ne '' } map { my $s = $_; $s =~ s/^\s+|\s+$//g; $s } split(/,/, $trimmed);
    return \@ids;
}

__PACKAGE__->register_method({
    name        => 'index',
    path        => '',
    method      => 'GET',
    description => 'Google Drive addon API index.',
    permissions => { user => 'all' },
    parameters  => {
        additionalProperties => 0,
        properties           => {},
    },
    returns => {
        type  => 'array',
        items => {
            type       => 'object',
            properties => {
                subdir => { type => 'string' },
            },
        },
        links => [ { rel => 'child', href => '{subdir}' } ],
    },
    code => sub {
        return [
            { subdir => 'accounts' },
            { subdir => 'oauth' },
            { subdir => 'relay' },
            { subdir => 'queue' },
            { subdir => 'picker' },
        ];
    },
});

__PACKAGE__->register_method({
    name        => 'list_accounts',
    path        => 'accounts',
    method      => 'GET',
    description => 'List configured Google Drive account profiles and cached quotas.',
    permissions => {
        check => ['perm', '/storage', ['Datastore.Audit', 'Datastore.Allocate'], any => 1],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {},
    },
    returns => {
        type       => 'object',
        properties => {
            accounts => { type => 'array' },
        },
    },
    code => sub {
        my ($param) = @_;
        return _ipc_request('GET', '/v1/accounts');
    },
});

__PACKAGE__->register_method({
    name        => 'oauth_start',
    path        => 'oauth/start',
    method      => 'POST',
    description => 'Start a Google OAuth2 relay or BYOC PKCE flow.',
    permissions => {
        check => ['or',
            ['perm', '/', ['Sys.Modify']],
            ['perm', '/storage', ['Datastore.Allocate']],
        ],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            mode => {
                description => 'OAuth mode: relay (1-click), pve_self (Proxmox-hosted relay), or byoc (own OAuth app).',
                type        => 'string',
                enum        => ['relay', 'pve_self', 'byoc'],
                default     => 'relay',
                optional    => 1,
            },
            client_id => {
                description => 'Google OAuth2 client ID (required for byoc mode).',
                type        => 'string',
                optional    => 1,
            },
            client_secret => {
                description => 'Google OAuth2 client secret (optional for PKCE / byoc mode).',
                type        => 'string',
                optional    => 1,
            },
            scope => {
                description => 'Requested Google Drive OAuth2 scope.',
                type        => 'string',
                enum        => ['drive.file', 'drive'],
                optional    => 1,
            },
            redirect_uri => {
                description => 'OAuth redirect URI override.',
                type        => 'string',
                optional    => 1,
            },
            ui_origin => {
                description => 'Proxmox Web UI origin (https://host:8006) for self-hosted callback default.',
                type        => 'string',
                optional    => 1,
            },
        },
    },
    returns => {
        type       => 'object',
        properties => {
            state        => { type => 'string' },
            auth_url     => { type => 'string' },
            redirect_uri => { type => 'string', optional => 1 },
            relay_source => { type => 'string', optional => 1 },
        },
    },
    code => sub {
        my ($param) = @_;
        my $payload = {
            mode => $param->{mode} // 'relay',
        };
        for my $k (qw(client_id client_secret scope redirect_uri ui_origin)) {
            $payload->{$k} = $param->{$k} if defined $param->{$k} && $param->{$k} ne '';
        }
        return _ipc_request('POST', '/v1/oauth/start', $payload);
    },
});

__PACKAGE__->register_method({
    name        => 'oauth_finish',
    path        => 'oauth/finish',
    method      => 'POST',
    description => 'Complete Google OAuth2 authorization and persist account profile.',
    permissions => {
        check => ['or',
            ['perm', '/', ['Sys.Modify']],
            ['perm', '/storage', ['Datastore.Allocate']],
        ],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            state => {
                description => 'OAuth state identifier returned by oauth/start.',
                type        => 'string',
            },
            code => {
                description => 'Authorization code (for BYOC or manual code flow).',
                type        => 'string',
                optional    => 1,
            },
            tokens_json => {
                description => 'Token payload JSON received from the OAuth relay callback.',
                type        => 'string',
                optional    => 1,
            },
            account_id => {
                description => 'Optional custom profile ID.',
                type        => 'string',
                pattern     => '[a-zA-Z0-9._@-]+',
                optional    => 1,
            },
        },
    },
    returns => {
        type       => 'object',
        properties => {
            account => { type => 'object' },
        },
    },
    code => sub {
        my ($param) = @_;
        my $payload = { state => $param->{state} };
        for my $k (qw(code tokens_json account_id)) {
            $payload->{$k} = $param->{$k} if defined $param->{$k} && $param->{$k} ne '';
        }
        return _ipc_request('POST', '/v1/oauth/finish', $payload);
    },
});

__PACKAGE__->register_method({
    name        => 'create_service_account',
    path        => 'accounts/service-account',
    method      => 'POST',
    description => 'Register a Google Service Account JSON credential profile.',
    permissions => {
        check => ['or',
            ['perm', '/', ['Sys.Modify']],
            ['perm', '/storage', ['Datastore.Allocate']],
        ],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            account_id => {
                description => 'Optional profile ID for this service account.',
                type        => 'string',
                pattern     => '[a-zA-Z0-9._@-]+',
                optional    => 1,
            },
            service_account_json => {
                description => 'Contents of the Google Service Account JSON key file.',
                type        => 'string',
            },
        },
    },
    returns => {
        type       => 'object',
        properties => {
            account => { type => 'object' },
        },
    },
    code => sub {
        my ($param) = @_;
        my $payload = {
            service_account_json => $param->{service_account_json},
        };
        $payload->{account_id} = $param->{account_id}
            if defined $param->{account_id} && $param->{account_id} ne '';
        return _ipc_request('POST', '/v1/accounts/service-account', $payload);
    },
});

__PACKAGE__->register_method({
    name        => 'delete_account',
    path        => 'accounts/{account}',
    method      => 'DELETE',
    description => 'Delete a configured Google Drive account profile.',
    permissions => {
        check => ['or',
            ['perm', '/', ['Sys.Modify']],
            ['perm', '/storage', ['Datastore.Allocate']],
        ],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            account => {
                description => 'Google Drive account profile ID.',
                type        => 'string',
                pattern     => '[a-zA-Z0-9._@-]+',
            },
        },
    },
    returns => { type => 'null' },
    code => sub {
        my ($param) = @_;
        my $enc = _url_encode($param->{account});
        _ipc_request('DELETE', "/v1/accounts/$enc");
        return undef;
    },
});

__PACKAGE__->register_method({
    name        => 'list_drives',
    path        => 'accounts/{account}/drives',
    method      => 'GET',
    description => 'List Google Shared Drives accessible to an account.',
    permissions => {
        check => ['perm', '/storage', ['Datastore.Audit', 'Datastore.Allocate'], any => 1],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            account => {
                description => 'Google Drive account profile ID.',
                type        => 'string',
                pattern     => '[a-zA-Z0-9._@-]+',
            },
        },
    },
    returns => {
        type       => 'object',
        properties => {
            drives => { type => 'array' },
        },
    },
    code => sub {
        my ($param) = @_;
        my $enc = _url_encode($param->{account});
        return _ipc_request('GET', "/v1/accounts/$enc/drives");
    },
});

__PACKAGE__->register_method({
    name        => 'get_queue',
    path        => 'queue',
    method      => 'GET',
    description => 'Get active Google Drive upload queue and VFS cache status.',
    permissions => {
        check => ['perm', '/storage', ['Datastore.Audit', 'Datastore.Allocate'], any => 1],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            storeid => get_standard_option('pve-storage-id', { optional => 1 }),
        },
    },
    returns => {
        type       => 'object',
        properties => {
            uploads  => { type => 'array' },
            storages => { type => 'object' },
        },
    },
    code => sub {
        my ($param) = @_;
        my $path = '/v1/queue';
        if (defined $param->{storeid} && $param->{storeid} ne '') {
            $path .= '?storeid=' . _url_encode($param->{storeid});
        }
        return _ipc_request('GET', $path);
    },
});

__PACKAGE__->register_method({
    name        => 'get_relay_config',
    path        => 'relay/config',
    method      => 'GET',
    description => 'Get Proxmox-hosted Google OAuth relay configuration (client_secret redacted).',
    permissions => {
        check => ['or',
            ['perm', '/', ['Sys.Audit']],
            ['perm', '/storage', ['Datastore.Audit', 'Datastore.Allocate'], any => 1],
        ],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {},
    },
    returns => {
        type       => 'object',
        properties => {
            enabled           => { type => 'boolean' },
            client_id         => { type => 'string' },
            has_client_secret => { type => 'boolean' },
            redirect_uri      => { type => 'string' },
            scope             => { type => 'string' },
        },
    },
    code => sub {
        return _ipc_request('GET', '/v1/relay/config');
    },
});

__PACKAGE__->register_method({
    name        => 'save_relay_config',
    path        => 'relay/config',
    method      => 'POST',
    description => 'Configure and host a Google OAuth relay directly inside Proxmox VE UI.',
    permissions => {
        check => ['perm', '/', ['Sys.Modify']],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            enabled => {
                description => 'Enable self-hosted Google OAuth relay in Proxmox UI.',
                type        => 'boolean',
            },
            client_id => {
                description => 'Google OAuth2 client ID.',
                type        => 'string',
                optional    => 1,
            },
            client_secret => {
                description => 'Google OAuth2 client secret (stored only in /etc/pve/priv/gdrive/relay.json).',
                type        => 'string',
                optional    => 1,
            },
            redirect_uri => {
                description => 'Authorized redirect URI (defaults to Proxmox UI /pve2/gdrive-oauth-callback.html).',
                type        => 'string',
                optional    => 1,
            },
            scope => {
                description => 'Default Google Drive OAuth2 scope for self-hosted relay.',
                type        => 'string',
                enum        => ['drive.file', 'drive'],
                default     => 'drive.file',
                optional    => 1,
            },
        },
    },
    returns => {
        type       => 'object',
        properties => {
            enabled           => { type => 'boolean' },
            client_id         => { type => 'string' },
            has_client_secret => { type => 'boolean' },
            redirect_uri      => { type => 'string' },
            scope             => { type => 'string' },
        },
    },
    code => sub {
        my ($param) = @_;
        my $payload = {
            enabled => $param->{enabled} ? JSON::true : JSON::false,
            scope   => $param->{scope} // 'drive.file',
        };
        for my $k (qw(client_id client_secret redirect_uri)) {
            $payload->{$k} = $param->{$k} if defined $param->{$k} && $param->{$k} ne '';
        }
        return _ipc_request('POST', '/v1/relay/config', $payload);
    },
});

__PACKAGE__->register_method({
    name        => 'picker_import',
    path        => 'picker/import',
    method      => 'POST',
    description => 'Import ISO or container template files selected via Google Picker into proxmox/template/.',
    permissions => {
        check => ['or',
            ['perm', '/', ['Sys.Modify']],
            ['perm', '/storage', ['Datastore.Allocate']],
        ],
    },
    parameters => {
        additionalProperties => 0,
        properties           => {
            storeid => get_standard_option('pve-storage-id'),
            account => {
                description => 'Google Drive account profile ID.',
                type        => 'string',
                pattern     => '[a-zA-Z0-9._@-]+',
                optional    => 1,
            },
            file_ids => {
                description => 'Google Drive file IDs (comma-separated or JSON array).',
                type        => 'string',
            },
            target_type => {
                description => 'Destination template type (iso or vztmpl).',
                type        => 'string',
                enum        => ['iso', 'vztmpl'],
                default     => 'iso',
                optional    => 1,
            },
            access_token => {
                description => 'Short-lived Google Picker OAuth access token.',
                type        => 'string',
                optional    => 1,
            },
        },
    },
    returns => {
        type => 'object',
    },
    code => sub {
        my ($param) = @_;
        my $ids = _parse_file_ids($param->{file_ids});
        die "at least one file_id is required\n" unless @$ids;

        my $payload = {
            storeid     => $param->{storeid},
            file_ids    => $ids,
            target_type => $param->{target_type} // 'iso',
        };
        for my $k (qw(account access_token)) {
            $payload->{$k} = $param->{$k} if defined $param->{$k} && $param->{$k} ne '';
        }
        return _ipc_request('POST', '/v1/picker/import', $payload, 300);
    },
});

1;
