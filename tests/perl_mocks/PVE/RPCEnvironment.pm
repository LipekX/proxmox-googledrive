package PVE::RPCEnvironment;

use strict;
use warnings;

my $INSTANCE;

sub get {
    my ($class) = @_;
    $INSTANCE ||= bless {
        user => 'root@pam',
        permissions => { '*' => 1 },
    }, $class;
    return $INSTANCE;
}

sub set_mock_permissions {
    my ($class, $perms, $user) = @_;
    my $self = $class->get();
    $self->{permissions} = $perms || {};
    $self->{user} = $user if defined $user;
}

sub get_user {
    my ($self) = @_;
    return $self->{user};
}

sub check {
    my ($self, $user, $path, $privs, $noerr) = @_;
    return 1 if $self->{permissions}->{'*'};
    for my $p (@$privs) {
        return 1 if $self->{permissions}->{"$path:$p"} || $self->{permissions}->{$p};
    }
    die "Permission check failed ($path, [" . join(',', @$privs) . "])\n" unless $noerr;
    return 0;
}

sub check_any {
    my ($self, $user, $path, $privs, $noerr) = @_;
    return 1 if $self->{permissions}->{'*'};
    for my $p (@$privs) {
        return 1 if $self->{permissions}->{"$path:$p"} || $self->{permissions}->{$p};
    }
    die "Permission check failed ($path, [" . join(',', @$privs) . "])\n" unless $noerr;
    return 0;
}

1;
