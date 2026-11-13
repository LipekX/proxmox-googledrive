package PVE::Storage::DirPlugin;

use strict;
use warnings;
use parent qw(PVE::Storage::Plugin);

sub type {
    return 'dir';
}

sub plugindata {
    return {
        content => [
            { images => 1, rootdir => 1, vztmpl => 1, iso => 1, backup => 1, snippets => 1, none => 1 },
            { images => 1, rootdir => 1 },
        ],
        format => [ { raw => 1, qcow2 => 1, vmdk => 1, subvol => 1 }, 'raw' ],
    };
}

1;
