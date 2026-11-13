package PVE::RESTHandler;

use strict;
use warnings;
use PVE::JSONSchema;
use PVE::RPCEnvironment;

my %METHODS;

sub register_method {
    my ($class, $info) = @_;
    $METHODS{$class} ||= [];
    push @{$METHODS{$class}}, $info;
}

sub registered_methods {
    my ($class) = @_;
    return $METHODS{$class} || [];
}

sub find_handler {
    my ($class, $method, $path) = @_;
    $path //= '';
    for my $entry (@{$METHODS{$class} || []}) {
        next if $entry->{subclass};
        next unless ($entry->{method} // '') eq $method;
        my $pattern = $entry->{path} // '';
        if ($pattern eq $path) {
            return ($entry, {});
        }
        my @pat_parts = split m!/!, $pattern;
        my @req_parts = split m!/!, $path;
        next unless @pat_parts == @req_parts;
        my %captured;
        my $match = 1;
        for my $i (0 .. $#pat_parts) {
            if ($pat_parts[$i] =~ /^\{([^}]+)\}$/) {
                $captured{$1} = $req_parts[$i];
            } elsif ($pat_parts[$i] ne $req_parts[$i]) {
                $match = 0;
                last;
            }
        }
        return ($entry, \%captured) if $match;
    }
    return;
}

sub _eval_perm {
    my ($rpcenv, $user, $perm) = @_;
    return 1 if !defined $perm || $perm->{user} && $perm->{user} eq 'all';
    my $check = $perm->{check};
    return 1 unless $check;
    return _eval_check_expr($rpcenv, $user, $check);
}

sub _eval_check_expr {
    my ($rpcenv, $user, $expr) = @_;
    return 1 unless ref($expr) eq 'ARRAY';
    my $op = $expr->[0];
    if ($op eq 'or') {
        my $last_err = "Permission check failed\n";
        for my $i (1 .. $#$expr) {
            my $ok = eval { _eval_check_expr($rpcenv, $user, $expr->[$i]); 1 };
            return 1 if $ok;
            $last_err = $@ if $@;
        }
        die $last_err;
    } elsif ($op eq 'and') {
        for my $i (1 .. $#$expr) {
            _eval_check_expr($rpcenv, $user, $expr->[$i]);
        }
        return 1;
    } elsif ($op eq 'perm') {
        my ($path, $privs, %opts) = @{$expr}[1 .. $#$expr];
        if ($opts{any}) {
            return $rpcenv->check_any($user, $path, $privs);
        }
        return $rpcenv->check($user, $path, $privs);
    }
    return 1;
}

sub test_call {
    my ($class, $method, $path, $param) = @_;
    $param = { %{$param || {}} };
    my ($entry, $captured) = $class->find_handler($method, $path);
    die "No handler registered for $method '$path' on $class\n" unless $entry;
    $param->{$_} = $captured->{$_} for keys %$captured;

    my $rpcenv = PVE::RPCEnvironment->get();
    my $user = $rpcenv->get_user();
    _eval_perm($rpcenv, $user, $entry->{permissions});

    if ($entry->{parameters}) {
        PVE::JSONSchema::validate($param, $entry->{parameters});
    }
    return $entry->{code}->($param);
}

1;
