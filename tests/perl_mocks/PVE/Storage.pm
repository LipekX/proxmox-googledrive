package PVE::Storage;

use strict;
use warnings;

our $MOCK_APIVER = 11;
our $MOCK_APIAGE = 2;

sub APIVER { return $MOCK_APIVER; }
sub APIAGE { return $MOCK_APIAGE; }

our $MOCK_CONFIG;

sub config {
    return $MOCK_CONFIG if defined $MOCK_CONFIG;
    return { ids => {} };
}

1;
