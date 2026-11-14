package PVE::Storage::Custom::GDrivePlugin;

use strict;
use warnings;

use parent qw(PVE::Storage::DirPlugin);

use IO::Socket::UNIX qw(SOCK_STREAM);
use IO::Select;
use File::Path qw(make_path);
use File::Basename qw(dirname);
use File::Temp qw(tempfile);
use JSON qw(encode_json decode_json);
use PVE::JSONSchema qw(get_standard_option);

use constant {
    MIN_APIVER          => 9,
    MAX_APIVER          => 18,
    DEFAULT_SOCK_PATH   => '/run/pve-gdrived.sock',
    DEFAULT_PRIV_DIR    => '/etc/pve/priv/gdrive',
    DEFAULT_MNT_DIR     => '/mnt/pve',
    DEFAULT_CACHE_DIR   => '/var/cache/pve-gdrive',
    STATUS_TIMEOUT_SEC  => 1.5,
    IPC_TIMEOUT_SEC     => 30,
};

my $API2_REGISTERED = 0;

sub _sock_path {
    return $ENV{PVE_GDRIVE_SOCK} || DEFAULT_SOCK_PATH;
}

sub _priv_dir {
    return $ENV{PVE_GDRIVE_PRIV_DIR} || DEFAULT_PRIV_DIR;
}

sub _mnt_dir {
    return $ENV{PVE_GDRIVE_MNT_DIR} || DEFAULT_MNT_DIR;
}

sub _crypt_file_path {
    my ($storeid) = @_;
    die "invalid storeid '$storeid'\n" if !defined($storeid) || $storeid !~ /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;
    return _priv_dir() . "/storages/$storeid.crypt.json";
}

sub _mount_path {
    my ($scfg, $storeid) = @_;
    if ($scfg && defined $scfg->{path} && $scfg->{path} ne '') {
        return $scfg->{path};
    }
    my $id = defined($storeid) && $storeid ne '' ? $storeid : ($scfg ? $scfg->{storeid} : undef);
    die "cannot determine mount path without path or storeid\n" unless defined $id && $id ne '';
    return _mnt_dir() . "/$id";
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

sub _write_crypt_passphrase {
    my ($storeid, $passphrase) = @_;
    my $path = _crypt_file_path($storeid);
    my $dir = dirname($path);
    my $is_pmxcfs = ($path =~ m!^/etc/pve(?:/|$)!);
    make_path($dir, { mode => 0700 }) unless -d $dir;
    chmod 0700, $dir unless $is_pmxcfs;

    my ($fh, $tmp) = tempfile('.crypt.XXXXXX', DIR => $dir, UNLINK => 0);
    if (!$is_pmxcfs) {
        chmod 0600, $tmp or die "chmod failed on $tmp: $!\n";
    }
    my $json = encode_json({
        storeid    => $storeid,
        passphrase => $passphrase,
    }) . "\n";
    print {$fh} $json or die "write failed on $tmp: $!\n";
    close($fh) or die "close failed on $tmp: $!\n";
    rename($tmp, $path) or do {
        unlink($tmp);
        die "rename $tmp -> $path failed: $!\n";
    };
    chmod 0600, $path unless $is_pmxcfs;
    return $path;
}

sub _storage_payload {
    my ($storeid, $scfg) = @_;
    return {
        storeid        => $storeid,
        account        => $scfg->{account} // '',
        drive_id       => $scfg->{drive_id} // '',
        subfolder      => $scfg->{subfolder} // '',
        root_folder_id => $scfg->{root_folder_id} // '',
        cache_dir      => $scfg->{cache_dir} || DEFAULT_CACHE_DIR,
        (defined $scfg->{cache_max_size} && $scfg->{cache_max_size} ne ''
            ? (cache_max_size => $scfg->{cache_max_size})
            : ()),
        async_upload   => $scfg->{async_upload} ? 1 : 0,
        encrypt        => $scfg->{encrypt} ? 1 : 0,
        path           => _mount_path($scfg, $storeid),
    };
}

sub api {
    my $apiver = eval { PVE::Storage::APIVER() }
        // eval { PVE::Storage::Plugin::APIVER() }
        // 10;
    my $apiage = eval { PVE::Storage::APIAGE() }
        // eval { PVE::Storage::Plugin::APIAGE() }
        // 0;
    my $min_accepted = $apiver - $apiage;

    if ($apiver >= MIN_APIVER && $min_accepted <= MAX_APIVER) {
        return $apiver <= MAX_APIVER ? $apiver : MAX_APIVER;
    }
    return $apiver;
}

sub type {
    return 'gdrive';
}

sub plugindata {
    return {
        content => [
            { backup => 1, iso => 1, vztmpl => 1 },
            { backup => 1 },
        ],
        'sensitive-properties' => {
            crypt_passphrase => 1,
        },
    };
}

sub properties {
    return {
        account => {
            description => 'Google Drive account profile ID.',
            type => 'string',
        },
        drive_id => {
            description => 'Google Shared Drive ID (omit or empty for My Drive).',
            type => 'string',
            optional => 1,
        },
        subfolder => {
            description => 'Optional subfolder inside proxmox/ on Google Drive.',
            type => 'string',
            optional => 1,
        },
        root_folder_id => {
            description => 'Pinned Google Drive folder ID for the proxmox/ storage root.',
            type => 'string',
            optional => 1,
        },
        cache_dir => {
            description => 'Local directory for rclone VFS cache.',
            type => 'string',
            format => 'pve-storage-path',
            default => DEFAULT_CACHE_DIR,
            optional => 1,
        },
        cache_max_size => {
            description => 'Maximum VFS cache size (e.g. 20G, 500M). Defaults to auto-cap.',
            type => 'string',
            optional => 1,
        },
        async_upload => {
            description => 'If 1, vzdump returns before Google Drive upload completes (default 0 waits).',
            type => 'boolean',
            default => 0,
            optional => 1,
        },
        encrypt => {
            description => 'Enable client-side rclone crypt encryption.',
            type => 'boolean',
            default => 0,
            optional => 1,
        },
        crypt_passphrase => {
            description => 'Passphrase for client-side rclone crypt encryption.',
            type => 'string',
            optional => 1,
        },
    };
}

sub options {
    return {
        path           => { optional => 1 },
        account        => { fixed => 1 },
        drive_id       => { optional => 1 },
        subfolder      => { optional => 1 },
        root_folder_id => { optional => 1 },
        cache_dir      => { optional => 1 },
        cache_max_size => { optional => 1 },
        async_upload   => { optional => 1 },
        encrypt        => { optional => 1 },
        crypt_passphrase => { optional => 1 },
        nodes          => { optional => 1 },
        disable        => { optional => 1 },
        content        => { optional => 1 },
        shared         => { optional => 1 },
        'prune-backups' => { optional => 1 },
    };
}

sub check_config {
    my ($self, $sectionId, $config, $create, $skipSchemaCheck) = @_;
    if ($create && (!defined($config->{path}) || $config->{path} eq '')) {
        $config->{path} = _mount_path($config, $sectionId);
    }
    return $self->SUPER::check_config($sectionId, $config, $create, $skipSchemaCheck);
}

sub path {
    my ($class, $scfg, $volname, $storeid, $snapname) = @_;
    my $mnt = _mount_path($scfg, $storeid);
    return $mnt unless defined $volname;
    my $local_scfg = { %$scfg, path => $mnt };
    return $class->filesystem_path($local_scfg, $volname, $snapname);
}

sub get_subdir {
    my ($class, $scfg, $volname, $storeid) = @_;
    my $mnt = _mount_path($scfg, $storeid);
    return $mnt unless defined $volname;
    my $local_scfg = { %$scfg, path => $mnt };
    return $class->SUPER::get_subdir($local_scfg, $volname);
}

sub filesystem_path {
    my ($class, $scfg, $volname, $snapname, $storeid) = @_;
    my $mnt = _mount_path($scfg, $storeid);
    my $local_scfg = { %$scfg, path => $mnt };
    return $class->SUPER::filesystem_path($local_scfg, $volname, $snapname);
}

sub status {
    my ($class, $storeid, $scfg, $cache) = @_;

    my $res = eval {
        _ipc_request('GET', "/v1/storage/$storeid/status", undef, STATUS_TIMEOUT_SEC);
    };
    if ($@ || !$res || ref($res) ne 'HASH') {
        return (0, 0, 0, 0);
    }

    my $active = $res->{active} ? 1 : 0;
    my $total  = int($res->{total} // 0);
    my $avail  = int($res->{avail} // 0);
    my $used   = int($res->{used}  // 0);

    return ($total, $avail, $used, $active);
}

sub on_add_hook {
    my ($class, $storeid, $scfg, %param) = @_;

    $scfg->{path} = _mount_path($scfg, $storeid)
        if !defined($scfg->{path}) || $scfg->{path} eq '';
    $scfg->{cache_dir} = DEFAULT_CACHE_DIR
        if !defined($scfg->{cache_dir}) || $scfg->{cache_dir} eq '';

    my $passphrase = delete $param{crypt_passphrase};
    my $scfg_pass  = delete $scfg->{crypt_passphrase};
    $passphrase = $scfg_pass if !defined($passphrase) || $passphrase eq '';

    if ($scfg->{encrypt}) {
        my $crypt_path = _crypt_file_path($storeid);
        if (defined($passphrase) && $passphrase ne '') {
            _write_crypt_passphrase($storeid, $passphrase);
        } elsif (!-f $crypt_path) {
            die "encrypt is enabled for storage '$storeid' but crypt_passphrase was not provided\n";
        }
    }

    my $res = _ipc_request('POST', '/v1/storage/init', _storage_payload($storeid, $scfg));
    if ($res && ref($res) eq 'HASH' && defined($res->{root_folder_id}) && $res->{root_folder_id} ne '') {
        $scfg->{root_folder_id} = $res->{root_folder_id};
    }

    return;
}

sub on_update_hook {
    my ($class, $storeid, $scfg, %param) = @_;

    $scfg->{path} = _mount_path($scfg, $storeid)
        if !defined($scfg->{path}) || $scfg->{path} eq '';

    my $passphrase = delete $param{crypt_passphrase};
    my $scfg_pass  = delete $scfg->{crypt_passphrase};
    $passphrase = $scfg_pass if !defined($passphrase) || $passphrase eq '';

    if (defined($passphrase) && $passphrase ne '') {
        _write_crypt_passphrase($storeid, $passphrase);
    } elsif ($scfg->{encrypt}) {
        my $crypt_path = _crypt_file_path($storeid);
        die "encrypt is enabled for storage '$storeid' and no crypt passphrase file exists\n"
            unless -f $crypt_path;
    }

    my $res = _ipc_request('POST', '/v1/storage/init', _storage_payload($storeid, $scfg));
    if ($res && ref($res) eq 'HASH' && defined($res->{root_folder_id}) && $res->{root_folder_id} ne '') {
        $scfg->{root_folder_id} = $res->{root_folder_id};
    }

    return;
}

sub on_update_hook_full {
    my ($class, $storeid, $scfg, $update, $delete, $sensitive) = @_;
    my %merged = ( %{$scfg || {}}, %{$update || {}} );
    if (ref($delete) eq 'ARRAY') {
        delete $merged{$_} for @$delete;
    }
    $class->on_update_hook($storeid, \%merged, %{ $sensitive || {} });
    if (defined $merged{root_folder_id}) {
        $update->{root_folder_id} = $merged{root_folder_id};
    }
    return;
}

sub on_delete_hook {
    my ($class, $storeid, $scfg) = @_;

    # Deactivate local mount only; NEVER delete remote Google Drive backups/ISOs.
    eval {
        _ipc_request('POST', '/v1/storage/deactivate', { storeid => $storeid });
    };

    my $crypt_path = eval { _crypt_file_path($storeid) };
    if ($crypt_path && -f $crypt_path) {
        unlink($crypt_path);
    }

    return;
}

sub activate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;

    my $mnt = _mount_path($scfg, $storeid);
    make_path($mnt) unless -d $mnt;

    _ipc_request('POST', '/v1/storage/activate', _storage_payload($storeid, $scfg));
    return 1;
}

sub deactivate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;

    _ipc_request('POST', '/v1/storage/deactivate', { storeid => $storeid });
    return 1;
}

sub register_api2 {
    return 1 if $API2_REGISTERED;
    my $ok = eval {
        if ($INC{'PVE/API2.pm'} || PVE::API2->can('register_method')) {
            require PVE::RESTHandler;
            if (!PVE::API2->isa('PVE::RESTHandler')) {
                no strict 'refs';
                push @{'PVE::API2::ISA'}, 'PVE::RESTHandler';
            }
            require PVE::API2::GDrive;
            my $already = 0;
            if (PVE::API2->can('method_attributes')) {
                my $ma = PVE::API2->method_attributes() || [];
                $already = grep { ($_->{path} // '') eq 'gdrive' } @$ma;
            }
            if (!$already) {
                PVE::API2->register_method({
                    subclass => 'PVE::API2::GDrive',
                    path     => 'gdrive',
                });
            }
            $API2_REGISTERED = 1;
        }
        1;
    };
    warn "pve-storage-gdrive register_api2 error: $@\n" if $@;
    return $ok ? 1 : 0;
}

# Register PVE::API2::GDrive automatically when loaded inside pvedaemon/pveproxy.
eval { __PACKAGE__->register_api2(); };

1;
