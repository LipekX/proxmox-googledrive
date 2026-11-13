#!/usr/bin/perl

use strict;
use warnings;

use Cwd qw(abs_path);
use File::Basename qw(dirname);
use File::Temp qw(tempdir);
use IO::Socket::UNIX qw(SOCK_STREAM SOMAXCONN);
use POSIX qw(WIFEXITED WEXITSTATUS);

my $ROOT;
BEGIN {
    $ROOT = abs_path(dirname(__FILE__) . '/..');
    unshift @INC, "$ROOT/tests/perl_mocks", "$ROOT/perl";
}

use JSON qw(encode_json decode_json);
use PVE::API2;
use PVE::Storage;
use PVE::RPCEnvironment;
use PVE::Storage::Custom::GDrivePlugin;
use PVE::API2::GDrive;

my $TOTAL = 0;
my $PASSED = 0;
my $FAILED = 0;

sub ok ($$) {
    my ($cond, $name) = @_;
    $TOTAL++;
    if ($cond) {
        $PASSED++;
        print "ok $TOTAL - $name\n";
        return 1;
    } else {
        $FAILED++;
        print "not ok $TOTAL - $name\n";
        return 0;
    }
}

sub is ($$$) {
    my ($got, $exp, $name) = @_;
    my $ok_cmp = (!defined($got) && !defined($exp))
        || (defined($got) && defined($exp) && "$got" eq "$exp");
    if (!ok($ok_cmp, $name)) {
        my $g = defined($got) ? "'$got'" : 'undef';
        my $e = defined($exp) ? "'$exp'" : 'undef';
        print STDERR "#   got: $g\n#   expected: $e\n";
    }
    return $ok_cmp;
}

sub like ($$$) {
    my ($got, $re, $name) = @_;
    my $ok_cmp = defined($got) && $got =~ $re;
    if (!ok($ok_cmp, $name)) {
        my $g = defined($got) ? "'$got'" : 'undef';
        print STDERR "#   got: $g\n#   expected match: $re\n";
    }
    return $ok_cmp;
}

sub _deep_eq {
    my ($a, $b) = @_;
    return 1 if !defined($a) && !defined($b);
    return 0 if !defined($a) || !defined($b);
    my ($ra, $rb) = (ref($a), ref($b));
    return "$a" eq "$b" if !$ra && !$rb;
    if ($ra eq 'JSON::PP::Boolean' || $rb eq 'JSON::PP::Boolean') {
        return ($a ? 1 : 0) == ($b ? 1 : 0);
    }
    return 0 unless $ra eq $rb;
    if ($ra eq 'HASH') {
        my @ka = sort keys %$a;
        my @kb = sort keys %$b;
        return 0 unless @ka == @kb;
        for my $i (0 .. $#ka) {
            return 0 unless $ka[$i] eq $kb[$i];
            return 0 unless _deep_eq($a->{$ka[$i]}, $b->{$kb[$i]});
        }
        return 1;
    }
    if ($ra eq 'ARRAY') {
        return 0 unless @$a == @$b;
        for my $i (0 .. $#$a) {
            return 0 unless _deep_eq($a->[$i], $b->[$i]);
        }
        return 1;
    }
    return "$a" eq "$b";
}

sub is_deeply ($$$) {
    my ($got, $exp, $name) = @_;
    my $eq = _deep_eq($got, $exp);
    if (!ok($eq, $name)) {
        print STDERR "#   got: " . encode_json($got) . "\n#   expected: " . encode_json($exp) . "\n";
    }
    return $eq;
}

# ---------------------------------------------------------------------------
# Mock Unix Domain Socket IPC Server
# ---------------------------------------------------------------------------
my $TMPDIR   = tempdir(CLEANUP => 1);
my $SOCK     = "$TMPDIR/pve-gdrived.sock";
my $LOG_FILE = "$TMPDIR/requests.log";
my $PRIV_DIR = "$TMPDIR/priv";
my $MNT_DIR  = "$TMPDIR/mnt";

local $ENV{PVE_GDRIVE_SOCK}     = $SOCK;
local $ENV{PVE_GDRIVE_PRIV_DIR} = $PRIV_DIR;
local $ENV{PVE_GDRIVE_MNT_DIR}  = $MNT_DIR;

sub read_requests {
    return [] unless -f $LOG_FILE;
    open(my $fh, '<', $LOG_FILE) or die $!;
    my @items;
    while (my $line = <$fh>) {
        chomp $line;
        next unless $line =~ /\S/;
        push @items, decode_json($line);
    }
    close($fh);
    return \@items;
}

sub clear_requests {
    unlink $LOG_FILE if -f $LOG_FILE;
}

sub start_mock_server {
    my $listen = IO::Socket::UNIX->new(
        Local  => $SOCK,
        Type   => SOCK_STREAM,
        Listen => SOMAXCONN,
    ) or die "cannot bind mock unix socket '$SOCK': $!\n";

    my $pid = fork();
    die "fork failed: $!\n" unless defined $pid;
    if ($pid == 0) {
        $SIG{TERM} = sub { exit 0 };
        $SIG{PIPE} = 'IGNORE';
        while (my $client = $listen->accept()) {
            my $raw = '';
            while (1) {
                my $buf = '';
                my $n = sysread($client, $buf, 65536);
                last if !$n;
                $raw .= $buf;
                if ($raw =~ /\r\n\r\n/s) {
                    my ($h, $b) = split(/\r\n\r\n/, $raw, 2);
                    if ($h =~ /^Content-Length:\s*(\d+)/mi) {
                        last if length($b) >= int($1);
                    } else {
                        last;
                    }
                }
            }
            my ($hdrs, $body) = split(/\r\n\r\n/, $raw, 2);
            $body //= '';
            my ($req_line) = split(/\r\n/, $hdrs // '', 2);
            my ($method, $path) = ($req_line =~ /^(\S+)\s+(\S+)/);
            $method //= '';
            $path   //= '';

            my $parsed_body = ($body =~ /\S/) ? eval { decode_json($body) } : undef;
            if (open(my $lfh, '>>', $LOG_FILE)) {
                print {$lfh} encode_json({ method => $method, path => $path, body => $parsed_body }) . "\n";
                close($lfh);
            }

            my ($code, $resp) = (200, {});
            if ($method eq 'GET' && $path eq '/v1/storage/gd-ok/status') {
                $resp = {
                    active     => JSON::true,
                    total      => 107374182400,
                    avail      => 85899345920,
                    used       => 21474836480,
                    mounted    => JSON::true,
                    cache_used => 1048576,
                    cache_max  => 21474836480,
                };
            } elsif ($method eq 'GET' && $path eq '/v1/storage/gd-slow/status') {
                sleep 3;
                $resp = { active => JSON::true, total => 100, avail => 50, used => 50 };
            } elsif ($method eq 'GET' && $path eq '/v1/storage/gd-down/status') {
                $resp = { active => JSON::false, total => 0, avail => 0, used => 0, mounted => JSON::false };
            } elsif ($method eq 'POST' && $path eq '/v1/storage/init') {
                $resp = { ok => JSON::true, root_folder_id => 'folder_pinned_123' };
            } elsif ($method eq 'POST' && $path eq '/v1/storage/activate') {
                if (($parsed_body->{storeid} // '') eq 'gd-fail') {
                    $code = 500;
                    $resp = { error => 'rclone mount failed' };
                } else {
                    $resp = { ok => JSON::true, mounted => JSON::true };
                }
            } elsif ($method eq 'POST' && $path eq '/v1/storage/deactivate') {
                $resp = { ok => JSON::true };
            } elsif ($method eq 'GET' && $path eq '/v1/accounts') {
                $resp = {
                    accounts => [
                        {
                            id    => 'user@example.com',
                            email => 'user@example.com',
                            mode  => 'relay',
                            scope => 'drive.file',
                            quota => { limit => 1000, usage => 200, free => 800 },
                        },
                    ],
                };
            } elsif ($method eq 'GET' && $path eq '/v1/relay/config') {
                $resp = {
                    enabled           => JSON::true,
                    client_id         => 'pve-self-cid',
                    has_client_secret => JSON::true,
                    redirect_uri      => 'https://pve.example.com:8006/pve2/gdrive-oauth-callback.html',
                    scope             => 'drive.file',
                };
            } elsif ($method eq 'POST' && $path eq '/v1/relay/config') {
                $resp = {
                    enabled           => ($parsed_body->{enabled} ? JSON::true : JSON::false),
                    client_id         => $parsed_body->{client_id} // 'pve-self-cid',
                    has_client_secret => JSON::true,
                    redirect_uri      => $parsed_body->{redirect_uri} // '',
                    scope             => $parsed_body->{scope} // 'drive.file',
                };
            } elsif ($method eq 'POST' && $path eq '/v1/oauth/start') {
                $resp = {
                    state    => 'st-abc123',
                    auth_url => 'https://accounts.google.com/o/oauth2/v2/auth?state=st-abc123',
                };
            } elsif ($method eq 'POST' && $path eq '/v1/oauth/finish') {
                $resp = {
                    account => {
                        id    => $parsed_body->{account_id} // 'user@example.com',
                        email => 'user@example.com',
                        mode  => 'relay',
                        scope => 'drive.file',
                    },
                };
            } elsif ($method eq 'POST' && $path eq '/v1/accounts/service-account') {
                $resp = {
                    account => {
                        id    => $parsed_body->{account_id} // 'sa-backup',
                        email => 'sa@proj.iam.gserviceaccount.com',
                        mode  => 'service_account',
                        scope => 'drive',
                    },
                };
            } elsif ($method eq 'DELETE' && $path eq '/v1/accounts/user%40example.com') {
                $resp = { deleted => JSON::true };
            } elsif ($method eq 'GET' && $path eq '/v1/accounts/user%40example.com/drives') {
                $resp = {
                    drives => [
                        { id => '', name => 'My Drive' },
                        { id => '0ABShared123', name => 'PVE Backups Shared' },
                    ],
                };
            } elsif ($method eq 'GET' && $path =~ m!^/v1/queue(?:\?storeid=(.*))?$!) {
                $resp = {
                    uploads  => [ { storeid => ($1 || 'gd-ok'), file => 'vzdump.vma.zst', state => 'uploading' } ],
                    storages => { 'gd-ok' => { cache_used => 1024 } },
                };
            } elsif ($method eq 'POST' && $path eq '/v1/picker/import') {
                $resp = {
                    imported => [
                        { file_id => 'fid1', name => 'debian.iso', status => 'queued' },
                    ],
                };
            } elsif ($method eq 'POST' && $path eq '/v1/uploads/wait') {
                if (($parsed_body->{path} // '') =~ /fail-upload/) {
                    $code = 500;
                    $resp = { error => 'upload aborted by drive API' };
                } elsif (($parsed_body->{path} // '') =~ /not-ok/) {
                    $resp = { ok => JSON::false, status => 'failed' };
                } else {
                    $resp = { ok => JSON::true, status => 'uploaded' };
                }
            } else {
                $code = 404;
                $resp = { error => "not found: $method $path" };
            }

            my $json_resp = encode_json($resp);
            my $len = do { use bytes; length($json_resp) };
            my $status_txt = $code == 200 ? 'OK' : ($code == 404 ? 'Not Found' : 'Internal Server Error');
            my $http = "HTTP/1.1 $code $status_txt\r\n"
                . "Content-Type: application/json\r\n"
                . "Content-Length: $len\r\n"
                . "Connection: close\r\n\r\n"
                . $json_resp;
            syswrite($client, $http);
            close($client);
        }
        exit 0;
    }
    close($listen);
    return $pid;
}

my $server_pid = start_mock_server();
END {
    if ($server_pid) {
        kill 'TERM', $server_pid;
        waitpid($server_pid, 0);
    }
}

# ---------------------------------------------------------------------------
# 1. GDrivePlugin unit tests
# ---------------------------------------------------------------------------
my $plugin = 'PVE::Storage::Custom::GDrivePlugin';

ok($plugin->isa('PVE::Storage::DirPlugin'), 'GDrivePlugin subclasses PVE::Storage::DirPlugin');
is($plugin->type(), 'gdrive', 'type() returns gdrive');

{
    local $PVE::Storage::MOCK_APIVER = 10;
    local $PVE::Storage::MOCK_APIAGE = 1;
    is($plugin->api(), 10, 'api() negotiates PVE 8.x APIVER=10');

    local $PVE::Storage::MOCK_APIVER = 12;
    local $PVE::Storage::MOCK_APIAGE = 2;
    is($plugin->api(), 12, 'api() negotiates PVE 9.x APIVER=12');

    local $PVE::Storage::MOCK_APIVER = 18;
    local $PVE::Storage::MOCK_APIAGE = 4;
    is($plugin->api(), 15, 'api() caps at MAX_APIVER within APIAGE window');
}

{
    my $pdata = $plugin->plugindata();
    my $allowed = $pdata->{content}->[0];
    ok($allowed->{backup} && $allowed->{iso} && $allowed->{vztmpl},
        'plugindata() allows backup, iso, vztmpl');
    ok(!$allowed->{images} && !$allowed->{rootdir},
        'plugindata() explicitly excludes images and rootdir');
    is(join(',', sort keys %$allowed), 'backup,iso,vztmpl',
        'plugindata() allows ONLY backup, iso, vztmpl');
}

{
    my $props = $plugin->properties();
    my $opts  = $plugin->options();
    for my $p (qw(account drive_id subfolder root_folder_id cache_dir cache_max_size async_upload encrypt)) {
        ok(exists $props->{$p}, "properties() defines $p");
        ok(exists $opts->{$p}, "options() includes $p");
    }
    for my $std (qw(path nodes disable content shared prune-backups maxfiles)) {
        ok(exists $opts->{$std}, "options() includes standard option $std");
    }
    is($props->{cache_dir}{default}, '/var/cache/pve-gdrive', 'cache_dir defaults to /var/cache/pve-gdrive');
    is($props->{async_upload}{default}, 0, 'async_upload defaults to 0');
    is($props->{encrypt}{default}, 0, 'encrypt defaults to 0');
}

{
    my $scfg = { account => 'user@example.com' };
    is($plugin->path($scfg, undef, 'gd-ok'), "$MNT_DIR/gd-ok", 'path() without volname maps to mount root');
    is($plugin->get_subdir($scfg, 'backup/vzdump-qemu-100-test.vma.zst', 'gd-ok'),
        "$MNT_DIR/gd-ok/dump", 'get_subdir() maps backup to dump');
    is($plugin->get_subdir($scfg, 'iso/debian.iso', 'gd-ok'),
        "$MNT_DIR/gd-ok/template/iso", 'get_subdir() maps iso to template/iso');
    is($plugin->get_subdir($scfg, 'vztmpl/alpine.tar.zst', 'gd-ok'),
        "$MNT_DIR/gd-ok/template/cache", 'get_subdir() maps vztmpl to template/cache');

    my ($fspath, $vmid, $vtype) = $plugin->filesystem_path(
        $scfg, 'backup/vzdump-qemu-100-test.vma.zst', undef, 'gd-ok'
    );
    is($fspath, "$MNT_DIR/gd-ok/dump/vzdump-qemu-100-test.vma.zst", 'filesystem_path() returns full backup path');
    is($vmid, 100, 'filesystem_path() returns parsed vmid in list context');
    is($vtype, 'backup', 'filesystem_path() returns vtype in list context');
}

{
    my ($tot, $avail, $used, $act) = $plugin->status('gd-ok', {}, {});
    is($tot, 107374182400, 'status() returns total from daemon');
    is($avail, 85899345920, 'status() returns avail from daemon');
    is($used, 21474836480, 'status() returns used from daemon');
    is($act, 1, 'status() returns active=1 when mounted');

    my ($dt, $da, $du, $dact) = $plugin->status('gd-down', {}, {});
    is($dact, 0, 'status() returns active=0 when daemon reports inactive');

    my $t0 = time();
    my ($st, $sa, $su, $sact) = $plugin->status('gd-slow', {}, {});
    my $elapsed = time() - $t0;
    is($sact, 0, 'status() returns inactive on slow/stalled daemon instead of dying');
    ok($elapsed <= 3, "status() enforces ~1.5s timeout (elapsed=${elapsed}s)");

    {
        local $ENV{PVE_GDRIVE_SOCK} = "$TMPDIR/missing.sock";
        my ($mt, $ma, $mu, $mact) = $plugin->status('gd-ok', {}, {});
        is($mact, 0, 'status() gracefully returns inactive when socket is missing');
    }
}

{
    clear_requests();
    my $scfg = {
        account => 'user@example.com',
        encrypt => 1,
    };

    eval { $plugin->on_add_hook('gd-crypt', { %$scfg }); };
    like($@, qr/crypt_passphrase/, 'on_add_hook requires crypt_passphrase when encrypt=1');

    $plugin->on_add_hook('gd-crypt', $scfg, crypt_passphrase => 'super-secret-pass');
    is($scfg->{path}, "$MNT_DIR/gd-crypt", 'on_add_hook sets default path');
    is($scfg->{root_folder_id}, 'folder_pinned_123', 'on_add_hook pins root_folder_id from daemon');
    ok(!exists $scfg->{crypt_passphrase}, 'on_add_hook strips crypt_passphrase from storage.cfg hash');

    my $crypt_file = "$PRIV_DIR/storages/gd-crypt.crypt.json";
    ok(-f $crypt_file, 'on_add_hook writes crypt.json file');
    my $mode = (stat($crypt_file))[2] & 07777;
    is(sprintf('%04o', $mode), '0600', 'crypt.json has 0600 permissions');

    open(my $cfh, '<', $crypt_file) or die $!;
    local $/;
    my $cdata = decode_json(<$cfh>);
    close($cfh);
    is($cdata->{passphrase}, 'super-secret-pass', 'crypt.json contains passphrase');

    # on_update_hook without passphrase keeps existing crypt.json
    $scfg->{cache_max_size} = '10G';
    $plugin->on_update_hook('gd-crypt', $scfg);
    ok(-f $crypt_file, 'on_update_hook preserves crypt.json when passphrase omitted');

    # activate_storage / deactivate_storage
    ok($plugin->activate_storage('gd-crypt', $scfg, {}), 'activate_storage succeeds');
    ok(-d "$MNT_DIR/gd-crypt", 'activate_storage ensures mount directory exists');
    eval { $plugin->activate_storage('gd-fail', { account => 'a' }, {}); };
    like($@, qr/rclone mount failed/, 'activate_storage dies with daemon error on failure');
    ok($plugin->deactivate_storage('gd-crypt', $scfg, {}), 'deactivate_storage succeeds');

    # on_delete_hook removes local crypt file and deactivates without deleting remote files
    clear_requests();
    $plugin->on_delete_hook('gd-crypt', $scfg);
    ok(!-f $crypt_file, 'on_delete_hook removes local crypt.json');
    my $reqs = read_requests();
    is($reqs->[0]{path}, '/v1/storage/deactivate', 'on_delete_hook only calls /v1/storage/deactivate');
}

{
    my $api2_methods = PVE::API2->registered_methods();
    my ($found) = grep { ($_->{subclass} // '') eq 'PVE::API2::GDrive' && ($_->{path} // '') eq 'gdrive' } @$api2_methods;
    ok(defined $found, 'GDrivePlugin safely registers PVE::API2::GDrive into PVE::API2');
}

# ---------------------------------------------------------------------------
# 2. PVE::API2::GDrive RESTHandler & RBAC unit tests
# ---------------------------------------------------------------------------
{
    PVE::RPCEnvironment->set_mock_permissions({ '*' => 1 }, 'root@pam');

    my $idx = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'GET', '');
    is(scalar(@$idx), 5, 'GET "" index returns 5 subdirs');

    my $accts = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'GET', 'accounts');
    is($accts->{accounts}[0]{email}, 'user@example.com', 'GET accounts returns daemon accounts');

    my $rcfg = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'GET', 'relay/config');
    is($rcfg->{client_id}, 'pve-self-cid', 'GET relay/config returns self-hosted relay config');
    ok(!exists $rcfg->{client_secret}, 'GET relay/config never exposes client_secret');

    clear_requests();
    my $rsave = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'relay/config', {
        enabled      => 1,
        client_id    => 'new-cid',
        client_secret => 'new-sec',
        redirect_uri => 'https://pve.example.com:8006/pve2/gdrive-oauth-callback.html',
        scope        => 'drive',
    });
    is($rsave->{client_id}, 'new-cid', 'POST relay/config updates relay settings');
    my $rreqs = read_requests();
    is($rreqs->[0]{path}, '/v1/relay/config', 'POST relay/config hits /v1/relay/config');

    clear_requests();
    my $oauth = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'oauth/start', {
        mode      => 'pve_self',
        ui_origin => 'https://pve.example.com:8006',
    });
    is($oauth->{state}, 'st-abc123', 'POST oauth/start returns state');
    my $reqs = read_requests();
    is($reqs->[0]{body}{mode}, 'pve_self', 'POST oauth/start forwards pve_self mode');
    is($reqs->[0]{body}{ui_origin}, 'https://pve.example.com:8006', 'POST oauth/start forwards ui_origin');

    my $fin = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'oauth/finish', {
        state      => 'st-abc123',
        tokens_json => '{"access_token":"x"}',
    });
    is($fin->{account}{email}, 'user@example.com', 'POST oauth/finish returns created account');

    my $sa = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'accounts/service-account', {
        account_id           => 'sa-backup',
        service_account_json => '{"type":"service_account"}',
    });
    is($sa->{account}{id}, 'sa-backup', 'POST accounts/service-account registers SA');

    my $drives = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'GET', 'accounts/user@example.com/drives');
    is($drives->{drives}[1]{id}, '0ABShared123', 'GET accounts/{account}/drives URL-encodes and returns drives');

    my $q = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'GET', 'queue', { storeid => 'gd-ok' });
    is($q->{uploads}[0]{storeid}, 'gd-ok', 'GET queue forwards storeid filter');

    clear_requests();
    my $imp = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'picker/import', {
        storeid     => 'gd-ok',
        account     => 'user@example.com',
        file_ids    => 'fid1,fid2',
        target_type => 'iso',
    });
    is($imp->{imported}[0]{file_id}, 'fid1', 'POST picker/import imports files');
    $reqs = read_requests();
    is_deeply($reqs->[0]{body}{file_ids}, ['fid1', 'fid2'], 'POST picker/import parses comma-separated file_ids');

    my $del = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'DELETE', 'accounts/user@example.com');
    ok(!defined $del, 'DELETE accounts/{account} succeeds');

    # Schema validation errors
    eval {
        PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'oauth/start', { mode => 'invalid' });
    };
    like($@, qr/invalid value/i, 'JSONSchema rejects invalid oauth/start mode');

    eval {
        PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'oauth/finish', {});
    };
    like($@, qr/required/i, 'JSONSchema rejects oauth/finish missing required state');

    # RBAC tests
    PVE::RPCEnvironment->set_mock_permissions({ '/storage:Datastore.Audit' => 1 }, 'auditor@pve');
    my $audit_accts = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'GET', 'accounts');
    is($audit_accts->{accounts}[0]{id}, 'user@example.com', 'RBAC allows Datastore.Audit on GET accounts');

    eval {
        PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'oauth/start', { mode => 'relay' });
    };
    like($@, qr/Permission check failed/, 'RBAC denies Datastore.Audit on POST oauth/start');

    PVE::RPCEnvironment->set_mock_permissions({ '/storage:Datastore.Allocate' => 1 }, 'admin@pve');
    my $alloc_oauth = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'oauth/start', { mode => 'relay' });
    is($alloc_oauth->{state}, 'st-abc123', 'RBAC allows Datastore.Allocate on POST oauth/start');

    eval {
        PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'relay/config', { enabled => 1, client_id => 'x' });
    };
    like($@, qr/Permission check failed/, 'RBAC denies Datastore.Allocate-only user on POST relay/config');

    PVE::RPCEnvironment->set_mock_permissions({ '/:Sys.Modify' => 1 }, 'sysadmin@pve');
    my $sys_relay = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'POST', 'relay/config', { enabled => 0 });
    is($sys_relay->{enabled}, JSON::false, 'RBAC allows Sys.Modify on POST relay/config');
    my $sys_del = PVE::RESTHandler::test_call('PVE::API2::GDrive', 'DELETE', 'accounts/user@example.com');
    ok(!defined $sys_del, 'RBAC allows Sys.Modify on DELETE accounts/{account}');
}

# ---------------------------------------------------------------------------
# 3. scripts/vzdump-gdrive-hook.pl subprocess tests
# ---------------------------------------------------------------------------
{
    my $storage_cfg = "$TMPDIR/storage.cfg";
    open(my $sfh, '>', $storage_cfg) or die $!;
    print {$sfh} <<'CFG';
dir: local
        path /var/lib/vz
        content iso,vztmpl,backup

gdrive: gd-sync
        path /mnt/pve/gd-sync
        account user@example.com
        async_upload 0
        content backup

gdrive: gd-async
        path /mnt/pve/gd-async
        account user@example.com
        async_upload 1
        content backup
CFG
    close($sfh);

    my $hook = "$ROOT/scripts/vzdump-gdrive-hook.pl";
    ok(-x $hook, 'vzdump-gdrive-hook.pl is executable (0755)');

    my $run_hook = sub {
        my ($phase, $env) = @_;
        my $out_file = "$TMPDIR/hook.out";
        my $err_file = "$TMPDIR/hook.err";
        my $pid = fork();
        die "fork failed: $!" unless defined $pid;
        if ($pid == 0) {
            local %ENV = (%ENV, PVE_STORAGE_CFG => $storage_cfg, %$env);
            open STDOUT, '>', $out_file;
            open STDERR, '>', $err_file;
            exec $^X, $hook, $phase;
            exit 127;
        }
        waitpid($pid, 0);
        my $rc = WIFEXITED($?) ? WEXITSTATUS($?) : 255;
        local $/;
        open my $ofh, '<', $out_file; my $out = <$ofh> // ''; close $ofh;
        open my $efh, '<', $err_file; my $err = <$efh> // ''; close $efh;
        return ($rc, $out, $err);
    };

    clear_requests();
    my ($rc1) = $run_hook->('job-start', { STOREID => 'gd-sync', TARGET => '/mnt/pve/gd-sync/dump/a.vma.zst' });
    is($rc1, 0, 'hook ignores non-backup-end phase');
    is(scalar(@{read_requests()}), 0, 'hook makes no IPC call on job-start');

    clear_requests();
    my ($rc2) = $run_hook->('backup-end', { STOREID => 'local', TARGET => '/var/lib/vz/dump/a.vma.zst' });
    is($rc2, 0, 'hook skips non-gdrive storage');
    is(scalar(@{read_requests()}), 0, 'hook makes no IPC call for dir storage');

    clear_requests();
    my ($rc3, $out3) = $run_hook->('backup-end', { STOREID => 'gd-async', TARGET => '/mnt/pve/gd-async/dump/a.vma.zst' });
    is($rc3, 0, 'hook skips upload wait when async_upload=1');
    like($out3, qr/async_upload=1/, 'hook logs async_upload bypass');
    is(scalar(@{read_requests()}), 0, 'hook makes no IPC call when async_upload=1');

    clear_requests();
    my ($rc4, $out4) = $run_hook->('backup-end', {
        STOREID => 'gd-sync',
        TARGET  => '/mnt/pve/gd-sync/dump/vzdump-qemu-100.vma.zst',
        PVE_GDRIVE_UPLOAD_TIMEOUT => '60',
    });
    is($rc4, 0, 'hook succeeds when upload confirmed in sync mode');
    like($out4, qr/confirmed Google Drive upload/, 'hook prints upload confirmation');
    my $reqs = read_requests();
    is($reqs->[0]{path}, '/v1/uploads/wait', 'hook calls POST /v1/uploads/wait');
    is($reqs->[0]{body}{storeid}, 'gd-sync', 'hook sends storeid');
    is($reqs->[0]{body}{path}, '/mnt/pve/gd-sync/dump/vzdump-qemu-100.vma.zst', 'hook sends target path');
    is($reqs->[0]{body}{timeout_sec}, 60, 'hook sends timeout_sec');

    my ($rc5, undef, $err5) = $run_hook->('backup-end', {
        STOREID => 'gd-sync',
        TARGET  => '/mnt/pve/gd-sync/dump/fail-upload.vma.zst',
    });
    ok($rc5 != 0, 'hook fails loudly (exit != 0) when daemon returns HTTP 500');
    like($err5, qr/upload aborted by drive API/, 'hook prints daemon failure reason to stderr');

    my ($rc6, undef, $err6) = $run_hook->('backup-end', {
        STOREID => 'gd-sync',
        TARGET  => '/mnt/pve/gd-sync/dump/not-ok.vma.zst',
    });
    ok($rc6 != 0, 'hook fails loudly when daemon returns ok=false');
    like($err6, qr/upload verification failed/, 'hook prints verification failure to stderr');

    my ($rc7, undef, $err7) = $run_hook->('backup-end', {
        STOREID         => 'gd-sync',
        TARGET          => '/mnt/pve/gd-sync/dump/ok.vma.zst',
        PVE_GDRIVE_SOCK => "$TMPDIR/nonexistent.sock",
    });
    ok($rc7 != 0, 'hook fails loudly when daemon socket is unreachable');
    like($err7, qr/cannot connect to pve-gdrived socket/, 'hook prints socket error to stderr');
}

print "1..$TOTAL\n";
print "# Passed $PASSED/$TOTAL tests\n";
exit($FAILED ? 1 : 0);
