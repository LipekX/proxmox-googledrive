#!/usr/bin/perl

use strict;
use warnings;

use IO::Socket::UNIX qw(SOCK_STREAM);
use IO::Select;

use constant {
    DEFAULT_SOCK_PATH   => '/run/pve-gdrived.sock',
    DEFAULT_STORAGE_CFG => '/etc/pve/storage.cfg',
    DEFAULT_TIMEOUT_SEC => 7200,
};

my $HAS_JSON = eval { require JSON; 1 } ? 1 : 0;

sub _json_escape {
    my ($s) = @_;
    $s //= '';
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord($1))/ge;
    return '"' . $s . '"';
}

sub _encode_json {
    my ($val) = @_;
    return JSON::encode_json($val) if $HAS_JSON;
    if (ref($val) eq 'HASH') {
        my @pairs;
        for my $k (sort keys %$val) {
            my $v = $val->{$k};
            my $enc;
            if (!defined $v) {
                $enc = 'null';
            } elsif ($v =~ /^-?(?:0|[1-9][0-9]*)$/ && $k eq 'timeout_sec') {
                $enc = 0 + $v;
            } else {
                $enc = _json_escape($v);
            }
            push @pairs, _json_escape($k) . ':' . $enc;
        }
        return '{' . join(',', @pairs) . '}';
    }
    return _json_escape("$val");
}

sub _decode_json {
    my ($raw) = @_;
    return JSON::decode_json($raw) if $HAS_JSON;
    my %res;
    while ($raw =~ /"([^"\\]*(?:\\.[^"\\]*)*)"\s*:\s*(true|false|null|-?\d+(?:\.\d+)?|"([^"\\]*(?:\\.[^"\\]*)*)")/g) {
        my ($k, $v, $sv) = ($1, $2, $3);
        if (defined $sv) {
            $sv =~ s/\\n/\n/g;
            $sv =~ s/\\r/\r/g;
            $sv =~ s/\\t/\t/g;
            $sv =~ s/\\"/"/g;
            $sv =~ s/\\\\/\\/g;
            $res{$k} = $sv;
        } elsif ($v eq 'true') {
            $res{$k} = 1;
        } elsif ($v eq 'false') {
            $res{$k} = 0;
        } elsif ($v eq 'null') {
            $res{$k} = undef;
        } else {
            $res{$k} = 0 + $v;
        }
    }
    return \%res;
}

sub parse_storage_cfg {
    my ($cfg_path) = @_;
    return {} unless defined($cfg_path) && -r $cfg_path;

    open(my $fh, '<', $cfg_path) or die "cannot read $cfg_path: $!\n";
    my %storages;
    my $cur_id;

    while (my $line = <$fh>) {
        chomp $line;
        $line =~ s/\r$//;
        next if $line =~ /^\s*(?:#.*)?$/;

        if ($line =~ /^([a-zA-Z0-9_-]+):\s*(\S+)\s*$/) {
            my ($type, $id) = ($1, $2);
            $cur_id = $id;
            $storages{$id} = { type => $type };
        } elsif (defined($cur_id) && $line =~ /^\s+(\S+)(?:\s+(.*?))?\s*$/) {
            my ($k, $v) = ($1, $2);
            $storages{$cur_id}{$k} = defined($v) ? $v : 1;
        }
    }
    close($fh);
    return \%storages;
}

sub lookup_storage_cfg {
    my ($storeid) = @_;
    my $cfg_path = $ENV{PVE_STORAGE_CFG} || DEFAULT_STORAGE_CFG;

    my $storages = eval { parse_storage_cfg($cfg_path) };
    if ($storages && exists $storages->{$storeid}) {
        return $storages->{$storeid};
    }

    my $pve_cfg = eval {
        require PVE::Storage;
        PVE::Storage::config();
    };
    if ($pve_cfg && ref($pve_cfg) eq 'HASH' && $pve_cfg->{ids} && exists $pve_cfg->{ids}{$storeid}) {
        return $pve_cfg->{ids}{$storeid};
    }

    return undef;
}

sub is_truthy {
    my ($val) = @_;
    return 0 unless defined $val;
    return 1 if $val =~ /^(?:1|true|yes|on)$/i;
    return 0;
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

sub ipc_post_wait {
    my ($sock_path, $storeid, $target, $timeout_sec) = @_;

    my $sock = IO::Socket::UNIX->new(
        Type    => SOCK_STREAM,
        Peer    => $sock_path,
        Timeout => 10,
    ) or die "cannot connect to pve-gdrived socket '$sock_path': $!\n";

    my $body = _encode_json({
        storeid     => $storeid,
        path        => $target,
        timeout_sec => int($timeout_sec),
    });
    my $body_len = do { use bytes; length($body) };

    my $req = "POST /v1/uploads/wait HTTP/1.1\r\n"
        . "Host: localhost\r\n"
        . "Connection: close\r\n"
        . "Accept: application/json\r\n"
        . "Content-Type: application/json\r\n"
        . "Content-Length: $body_len\r\n"
        . "\r\n"
        . $body;

    my $sel = IO::Select->new($sock);
    my $written = 0;
    my $req_len = do { use bytes; length($req) };
    while ($written < $req_len) {
        die "timed out writing to pve-gdrived\n" unless $sel->can_write(10);
        my $n = syswrite($sock, $req, $req_len - $written, $written);
        die "write error to pve-gdrived: $!\n" unless defined $n;
        $written += $n;
    }

    my $now = sub { eval { require Time::HiRes; Time::HiRes::time() } // time() };
    my $raw = '';
    my $deadline = $now->() + $timeout_sec + 5;
    while (1) {
        my $rem = $deadline - $now->();
        die "timed out waiting for Google Drive upload confirmation\n" if $rem <= 0;
        die "timed out waiting for Google Drive upload confirmation\n" unless $sel->can_read($rem);
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

    my $data = ($resp_body =~ /\S/) ? eval { _decode_json($resp_body) } : {};
    if ($status_code >= 400) {
        my $msg = ($data && ref($data) eq 'HASH')
            ? ($data->{error} // $data->{message} // $status_reason)
            : $status_reason;
        die "pve-gdrived returned HTTP $status_code: $msg\n";
    }

    if ($data && ref($data) eq 'HASH') {
        if (defined($data->{error}) && $data->{error} ne '') {
            die "upload failed: $data->{error}\n";
        }
        if (exists($data->{ok}) && !$data->{ok}) {
            die "upload did not complete cleanly\n";
        }
        if (defined($data->{status}) && $data->{status} =~ /^(?:failed|error|timeout)$/i) {
            die "upload status: $data->{status}\n";
        }
    }

    return $data;
}

sub main {
    my ($phase) = @ARGV;
    return 0 unless defined($phase) && $phase eq 'backup-end';

    my $storeid = $ENV{STOREID};
    my $target  = $ENV{TARGET};
    return 0 unless defined($storeid) && $storeid ne '' && defined($target) && $target ne '';

    my $scfg = lookup_storage_cfg($storeid);
    return 0 unless $scfg && ($scfg->{type} // '') eq 'gdrive';

    if (is_truthy($scfg->{async_upload})) {
        print "INFO: vzdump-gdrive-hook: async_upload=1 for '$storeid'; skipping upload wait for $target\n";
        return 0;
    }

    my $sock_path   = $ENV{PVE_GDRIVE_SOCK} || DEFAULT_SOCK_PATH;
    my $timeout_sec = $ENV{PVE_GDRIVE_UPLOAD_TIMEOUT} || DEFAULT_TIMEOUT_SEC;

    print "INFO: vzdump-gdrive-hook: waiting for Google Drive upload of $target (storage '$storeid')...\n";
    my $res = eval { ipc_post_wait($sock_path, $storeid, $target, $timeout_sec) };
    if ($@) {
        print STDERR "ERROR: vzdump-gdrive-hook: Google Drive upload verification failed for $target: $@";
        return 1;
    }

    print "INFO: vzdump-gdrive-hook: confirmed Google Drive upload for $target\n";
    return 0;
}

exit(main()) unless caller;

1;
