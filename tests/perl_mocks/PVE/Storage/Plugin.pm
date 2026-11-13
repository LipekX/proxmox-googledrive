package PVE::Storage::Plugin;

use strict;
use warnings;

my $DEFAULT_SUBDIRS = {
    images  => 'images',
    rootdir => 'private',
    iso     => 'template/iso',
    vztmpl  => 'template/cache',
    backup  => 'dump',
    snippets => 'snippets',
};

sub parse_volname {
    my ($class, $volname) = @_;
    if ($volname =~ m!^backup/([^/]+)$!) {
        my $name = $1;
        my $vmid = ($name =~ /^vzdump-(?:qemu|lxc)-(\d+)-/) ? int($1) : undef;
        return ('backup', $name, $vmid, undef, undef, undef, 'raw');
    } elsif ($volname =~ m!^iso/([^/]+\.iso)$!i) {
        return ('iso', $1, undef, undef, undef, undef, 'raw');
    } elsif ($volname =~ m!^vztmpl/([^/]+)$!) {
        return ('vztmpl', $1, undef, undef, undef, undef, 'raw');
    } elsif ($volname =~ m!^(\d+)/([^/]+)$!) {
        return ('images', $2, int($1), undef, undef, undef, 'raw');
    }
    die "unable to parse volume name '$volname'\n";
}

sub get_subdir {
    my ($class, $scfg, $volname) = @_;
    my $path = $scfg->{path} // die "storage path not configured\n";
    my ($vtype) = $class->parse_volname($volname);
    my $dir = $DEFAULT_SUBDIRS->{$vtype} // die "unknown volume type '$vtype'\n";
    return "$path/$dir";
}

sub filesystem_path {
    my ($class, $scfg, $volname, $snapname) = @_;
    my ($vtype, $name, $vmid) = $class->parse_volname($volname);
    my $dir = $class->get_subdir($scfg, $volname);
    my $path = "$dir/$name";
    return wantarray ? ($path, $vmid, $vtype) : $path;
}

sub path {
    my ($class, $scfg, $volname, $storeid, $snapname) = @_;
    return $class->filesystem_path($scfg, $volname, $snapname);
}

sub activate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;
    return 1;
}

sub deactivate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;
    return 1;
}

sub status {
    my ($class, $storeid, $scfg, $cache) = @_;
    return (0, 0, 0, 0);
}

1;
