package PVE::JSONSchema;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(get_standard_option register_standard_option validate);

my %STANDARD_OPTIONS = (
    'pve-storage-id' => {
        description => 'The storage identifier.',
        type => 'string',
        format => 'pve-storage-id',
        minLength => 1,
        maxLength => 100,
    },
    'pve-storage-path' => {
        description => 'File system path.',
        type => 'string',
        format => 'pve-storage-path',
    },
    'pve-node-list' => {
        description => 'List of cluster node names.',
        type => 'string',
        format => 'pve-node-list',
    },
    'pve-storage-content' => {
        description => 'Allowed content types.',
        type => 'string',
        format => 'pve-storage-content-list',
    },
    'prune-backups' => {
        description => 'The retention options with shorter intervals are processed first with --keep-last being the very first one.',
        type => 'string',
        optional => 1,
    },
);

sub register_standard_option {
    my ($name, $schema) = @_;
    $STANDARD_OPTIONS{$name} = $schema;
}

sub get_standard_option {
    my ($name, $override) = @_;
    my $base = $STANDARD_OPTIONS{$name}
        // { type => 'string', description => "Standard option $name" };
    my %copy = %$base;
    if ($override && ref($override) eq 'HASH') {
        $copy{$_} = $override->{$_} for keys %$override;
    }
    return \%copy;
}

sub validate {
    my ($param, $schema) = @_;
    die "Parameters must be a hash\n" unless ref($param) eq 'HASH';
    my $props = $schema->{properties} || {};
    for my $k (sort keys %$props) {
        my $spec = $props->{$k};
        if (!$spec->{optional} && (!exists $param->{$k} || !defined $param->{$k} || $param->{$k} eq '')) {
            die "Parameter '$k' is required\n";
        }
        next unless exists $param->{$k} && defined $param->{$k};
        my $val = $param->{$k};
        if ($spec->{enum}) {
            my %allowed = map { $_ => 1 } @{$spec->{enum}};
            die "Parameter '$k' has invalid value '$val'\n" unless $allowed{$val};
        }
        if ($spec->{pattern}) {
            my $re = $spec->{pattern};
            die "Parameter '$k' does not match pattern $re\n" unless $val =~ /^$re$/;
        }
        if (($spec->{type} // '') eq 'boolean') {
            die "Parameter '$k' must be boolean (0 or 1)\n" unless $val =~ /^(?:0|1)$/;
        }
    }
    if (!$schema->{additionalProperties}) {
        for my $k (sort keys %$param) {
            die "Unknown parameter '$k'\n" unless exists $props->{$k};
        }
    }
    return 1;
}

1;
