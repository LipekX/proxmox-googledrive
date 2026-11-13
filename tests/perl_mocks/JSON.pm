package JSON;

use strict;
use warnings;
use Exporter qw(import);
use Scalar::Util qw(blessed looks_like_number);
use Encode qw(encode_utf8 decode_utf8);

our @EXPORT = qw(encode_json decode_json to_json from_json);

{
    package JSON::PP::Boolean;
    use overload
        'bool' => sub { ${$_[0]} ? 1 : 0 },
        '0+'   => sub { ${$_[0]} ? 1 : 0 },
        '""'   => sub { ${$_[0]} ? '1' : '0' },
        fallback => 1;
}

my $TRUE  = bless \(my $t = 1), 'JSON::PP::Boolean';
my $FALSE = bless \(my $f = 0), 'JSON::PP::Boolean';

sub true  () { return $TRUE; }
sub false () { return $FALSE; }
sub is_bool { my ($v) = @_; return blessed($v) && $v->isa('JSON::PP::Boolean'); }

sub new {
    my ($class) = @_;
    return bless { utf8 => 0, allow_nonref => 1, canonical => 0 }, $class;
}

sub utf8         { my ($self, $v) = @_; $self->{utf8} = defined($v) ? $v : 1; return $self; }
sub allow_nonref { my ($self, $v) = @_; $self->{allow_nonref} = defined($v) ? $v : 1; return $self; }
sub canonical    { my ($self, $v) = @_; $self->{canonical} = defined($v) ? $v : 1; return $self; }

sub encode {
    my ($self, $data) = @_;
    my $out = _encode_val($data, $self->{canonical});
    return $self->{utf8} ? encode_utf8($out) : $out;
}

sub decode {
    my ($self, $str) = @_;
    die "undef passed to JSON::decode\n" unless defined $str;
    $str = decode_utf8($str) if $self->{utf8} && !utf8::is_utf8($str);
    return _decode_str($str);
}

sub encode_json ($) {
    return __PACKAGE__->new->utf8(1)->encode($_[0]);
}

sub decode_json ($) {
    return __PACKAGE__->new->utf8(1)->decode($_[0]);
}

sub to_json ($;$) {
    my ($data, $opts) = @_;
    my $j = __PACKAGE__->new;
    $j->canonical($opts->{canonical}) if $opts && exists $opts->{canonical};
    return $j->encode($data);
}

sub from_json ($;$) {
    my ($str) = @_;
    return __PACKAGE__->new->decode($str);
}

sub _escape_str {
    my ($s) = @_;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\x08/\\b/g;
    $s =~ s/\x0c/\\f/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord($1))/ge;
    return '"' . $s . '"';
}

sub _encode_val {
    my ($val, $canonical) = @_;
    return 'null' unless defined $val;

    if (blessed($val)) {
        if ($val->isa('JSON::PP::Boolean')) {
            return $$val ? 'true' : 'false';
        }
        die "cannot encode blessed object of type " . ref($val) . "\n";
    }

    my $ref = ref($val);
    if ($ref eq 'HASH') {
        my @keys = $canonical ? sort keys %$val : keys %$val;
        my @parts = map { _escape_str($_) . ':' . _encode_val($val->{$_}, $canonical) } @keys;
        return '{' . join(',', @parts) . '}';
    }
    if ($ref eq 'ARRAY') {
        my @parts = map { _encode_val($_, $canonical) } @$val;
        return '[' . join(',', @parts) . ']';
    }
    if ($ref eq 'SCALAR') {
        return $$val ? 'true' : 'false';
    }

    # Distinguish string vs numeric scalar using B::svref_2object flags when available
    if (eval { require B; 1 }) {
        my $sv = B::svref_2object(\$val);
        my $flags = $sv->FLAGS;
        if (($flags & (B::SVf_IOK() | B::SVf_NOK())) && !($flags & B::SVf_POK())) {
            return 0 + $val;
        }
    }
    return _escape_str("$val");
}

sub _decode_str {
    my ($json) = @_;
    my $pos = 0;
    my $len = length($json);

    my $skip_ws = sub {
        while ($pos < $len && substr($json, $pos, 1) =~ /[ \t\r\n]/) {
            $pos++;
        }
    };

    my $parse_string = sub {
        die "expected '\"' at $pos\n" unless substr($json, $pos, 1) eq '"';
        $pos++;
        my $out = '';
        while ($pos < $len) {
            my $ch = substr($json, $pos, 1);
            $pos++;
            if ($ch eq '"') {
                return $out;
            }
            if ($ch eq '\\') {
                die "unterminated escape\n" if $pos >= $len;
                my $esc = substr($json, $pos, 1);
                $pos++;
                if    ($esc eq '"' || $esc eq '\\' || $esc eq '/') { $out .= $esc; }
                elsif ($esc eq 'b') { $out .= "\b"; }
                elsif ($esc eq 'f') { $out .= "\f"; }
                elsif ($esc eq 'n') { $out .= "\n"; }
                elsif ($esc eq 'r') { $out .= "\r"; }
                elsif ($esc eq 't') { $out .= "\t"; }
                elsif ($esc eq 'u') {
                    my $hex = substr($json, $pos, 4);
                    die "invalid \\u escape\n" unless length($hex) == 4 && $hex =~ /^[0-9a-fA-F]{4}$/;
                    $pos += 4;
                    my $cp = hex($hex);
                    if ($cp >= 0xD800 && $cp <= 0xDBFF && substr($json, $pos, 2) eq '\\u') {
                        my $hex2 = substr($json, $pos + 2, 4);
                        if (length($hex2) == 4 && $hex2 =~ /^[0-9a-fA-F]{4}$/) {
                            my $cp2 = hex($hex2);
                            if ($cp2 >= 0xDC00 && $cp2 <= 0xDFFF) {
                                $pos += 6;
                                $cp = 0x10000 + (($cp - 0xD800) << 10) + ($cp2 - 0xDC00);
                            }
                        }
                    }
                    $out .= chr($cp);
                } else {
                    die "invalid escape \\$esc\n";
                }
            } else {
                $out .= $ch;
            }
        }
        die "unterminated string\n";
    };

    my $parse_value;
    $parse_value = sub {
        $skip_ws->();
        die "unexpected end of JSON\n" if $pos >= $len;
        my $ch = substr($json, $pos, 1);

        if ($ch eq '"') {
            return $parse_string->();
        }
        if ($ch eq '{') {
            $pos++;
            $skip_ws->();
            my %obj;
            if ($pos < $len && substr($json, $pos, 1) eq '}') {
                $pos++;
                return \%obj;
            }
            while (1) {
                $skip_ws->();
                my $key = $parse_string->();
                $skip_ws->();
                die "expected ':' at $pos\n" unless substr($json, $pos, 1) eq ':';
                $pos++;
                $obj{$key} = $parse_value->();
                $skip_ws->();
                my $sep = substr($json, $pos, 1);
                $pos++;
                last if $sep eq '}';
                die "expected ',' or '}' at $pos\n" unless $sep eq ',';
            }
            return \%obj;
        }
        if ($ch eq '[') {
            $pos++;
            $skip_ws->();
            my @arr;
            if ($pos < $len && substr($json, $pos, 1) eq ']') {
                $pos++;
                return \@arr;
            }
            while (1) {
                push @arr, $parse_value->();
                $skip_ws->();
                my $sep = substr($json, $pos, 1);
                $pos++;
                last if $sep eq ']';
                die "expected ',' or ']' at $pos\n" unless $sep eq ',';
            }
            return \@arr;
        }
        if (substr($json, $pos, 4) eq 'true')  { $pos += 4; return $TRUE; }
        if (substr($json, $pos, 5) eq 'false') { $pos += 5; return $FALSE; }
        if (substr($json, $pos, 4) eq 'null')  { $pos += 4; return undef; }

        my $rest = substr($json, $pos);
        if ($rest =~ /^(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)/) {
            my $num = $1;
            $pos += length($num);
            return 0 + $num;
        }
        die "unexpected token at $pos: " . substr($json, $pos, 20) . "\n";
    };

    my $res = $parse_value->();
    $skip_ws->();
    die "trailing characters at $pos\n" if $pos < $len;
    return $res;
}

1;
