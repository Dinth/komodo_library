#!/usr/bin/perl
# Purpose: Scrutiny `script://` notifier. Scrutiny's own alert names only the
#   device, serial and failure type. This looks the drive up in Scrutiny's API
#   and sends a Signal message with its role, model, the failing attributes
#   (value against threshold), power-on time, temperature and a dashboard link.
#   If the lookup fails, it sends Scrutiny's stock text instead, so an alert is
#   never lost.
# Dependencies: perl-base and curl, both already in the scrutiny web image (no
#   CPAN modules, no jq). Scrutiny passes the alert in SCRUTINY_* env vars.
#   Settings: SIGNAL_NUMBER (sender), SIGNAL_RECIPIENT (number or group.<id>),
#   optional SIGNAL_API_URL, SCRUTINY_HOST_LABEL, SCRUTINY_DASHBOARD_URL,
#   SCRUTINY_API_URL. DRY_RUN=1 prints the message instead of sending it.
# Author: AI (Claude), 2026-11-01
use strict;
use warnings;

# Scrutiny passes UTF-8 bytes; work in characters and encode once when sending
utf8::decode($ENV{$_}) for grep { /^SCRUTINY_/ } keys %ENV;

my $API       = $ENV{SCRUTINY_API_URL}       || 'http://localhost:8080/api';
my $DASHBOARD = $ENV{SCRUTINY_DASHBOARD_URL} || 'http://10.10.1.13:93';
my $SIGNAL    = $ENV{SIGNAL_API_URL}         || 'http://signal-rest-api:8080';
my $HOST      = $ENV{SCRUTINY_HOST_ID} || $ENV{SCRUTINY_HOST_LABEL} || '';
my $TYPE      = $ENV{SCRUTINY_FAILURE_TYPE} || '';

my %TITLE = (
    SmartFailure    => "\x{1F534} Drive FAILED its SMART check",
    ScrutinyFailure => "\x{1F7E0} Drive failing (Scrutiny thresholds)",
    MissedUpload    => "\x{23F0} No SMART data from drive",
    CollectorError  => "\x{26A0}\x{FE0F} Scrutiny collector error",
    EmailTest       => "\x{2705} Scrutiny test notification",
);
# Bit flags of an attribute's `status` (pkg.AttributeStatus*)
my @STATUS = ([1, 'SMART fail'], [4, 'Scrutiny fail'], [2, 'warning']);
# Lines of Scrutiny's stock message that this script already shows in its own form
my $STOCK_FIELD = qr/^(Scrutiny .* notification.*|Host Id|Friendly Name|Device Name|Device Serial|Device Type|Failure Type|Date):?/;

# --- minimal JSON (perl-base ships no JSON module) ---------------------------

my $json;    # text being decoded; parse position is pos($json)

# decode_json: parse a JSON document into perl hashes/arrays/scalars
sub decode_json {
    ($json) = @_;
    pos($json) = 0;
    my $value = json_value();
    $json =~ /\G\s*\z/gc or die "trailing data in JSON\n";
    return $value;
}

# json_value: parse the JSON value at the current position
sub json_value {
    $json =~ /\G\s*/gc;
    if ($json =~ /\G\{/gc) {
        my %h;
        $json =~ /\G\s*/gc;
        return \%h if $json =~ /\G\}/gc;
        while (1) {
            $json =~ /\G\s*/gc;
            my $k = json_string();
            $json =~ /\G\s*:/gc or die "expected ':' in JSON object\n";
            $h{$k} = json_value();
            $json =~ /\G\s*/gc;
            next if $json =~ /\G,/gc;
            return \%h if $json =~ /\G\}/gc;
            die "expected ',' or '}' at offset " . pos($json) . "\n";
        }
    }
    if ($json =~ /\G\[/gc) {
        my @a;
        $json =~ /\G\s*/gc;
        return \@a if $json =~ /\G\]/gc;
        while (1) {
            push @a, json_value();
            $json =~ /\G\s*/gc;
            next if $json =~ /\G,/gc;
            return \@a if $json =~ /\G\]/gc;
            die "expected ',' or ']' at offset " . pos($json) . "\n";
        }
    }
    # A character test, not /\G(?=")/gc: perl refuses a second zero-length
    # /g match at the position where /\G\s*/gc just matched empty
    return json_string() if substr($json, pos($json), 1) eq '"';
    return $1 if $json =~ /\G(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)/gc;
    return 1 if $json =~ /\Gtrue/gc;
    return 0 if $json =~ /\Gfalse/gc;
    return undef if $json =~ /\Gnull/gc;
    die "unexpected JSON at offset " . pos($json) . "\n";
}

# json_string: parse a JSON string literal
sub json_string {
    $json =~ /\G"((?:[^"\\]|\\.)*)"/gcs or die "expected a JSON string\n";
    my $s = $1;
    my %esc = (n => "\n", t => "\t", r => "\r", b => "\b", f => "\f");
    $s =~ s{\\(?:u([0-9a-fA-F]{4})|(.))}{
        if (defined $1) { chr(hex $1) }
        else { exists $esc{$2} ? $esc{$2} : $2 }
    }ge;
    return $s;
}

# json_quote: encode a string as a JSON string literal
sub json_quote {
    my ($s) = @_;
    $s =~ s/(["\\])/\\$1/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\t/\\t/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord $1)/ge;
    return "\"$s\"";
}

# --- Scrutiny lookups --------------------------------------------------------

# get_json: GET a Scrutiny API path and decode the response
sub get_json {
    my ($path) = @_;
    open(my $fh, '-|', 'curl', '-fsS', '--max-time', '10', "$API$path")
        or die "cannot run curl: $!\n";
    my $body = do { local $/; <$fh> };
    close($fh) or die "GET $path failed\n";
    utf8::decode($body);
    return decode_json($body);
}

# find_device: return (uuid, details) of the alerting drive, or () if unknown
sub find_device {
    my $serial = $ENV{SCRUTINY_DEVICE_SERIAL} // '';
    return () unless length $serial;
    my $summary = get_json('/summary')->{data}{summary} || {};
    # Stale registrations can share a serial; prefer the one that has SMART data
    my @uuids = sort { defined($summary->{$b}{smart}) <=> defined($summary->{$a}{smart}) }
                grep { ($summary->{$_}{device}{serial_number} // '') eq $serial }
                keys %$summary;
    return () unless @uuids;
    return ($uuids[0], get_json("/device/$uuids[0]/details"));
}

# status_text: describe an attribute status bitmask
sub status_text {
    my ($status) = @_;
    return join '+', map { $_->[1] } grep { $status & $_->[0] } @STATUS;
}

# failing_attributes: one line per attribute with a non-zero status, worst first
sub failing_attributes {
    my ($details) = @_;
    my $result = $details->{data}{smart_results}[0] or return ();
    my $attrs  = $result->{attrs} || {};
    my $meta   = $details->{metadata} || {};
    my $ata    = ($details->{data}{device}{device_protocol} // '') eq 'ATA';
    my @lines;
    for my $id (sort { ($attrs->{$b}{status} // 0) <=> ($attrs->{$a}{status} // 0) || $a cmp $b }
                keys %$attrs) {
        my $attr = $attrs->{$id};
        next unless ($attr->{status} // 0) > 0;
        my $name = $meta->{$id}{display_name} // $id;
        my $thresh = $attr->{thresh} // -1;
        my $value = $ata
            ? 'raw ' . ($attr->{raw_string} // $attr->{raw_value} // '?')
              . " (normalised $attr->{value}" . ($thresh >= 0 ? ", threshold $thresh" : '') . ')'
            : ($attr->{value} // '?') . ($thresh >= 0 ? " (threshold $thresh)" : '');
        push @lines, "\x{2022} $name: $value [" . status_text($attr->{status}) . ']';
    }
    return @lines;
}

# human_size: bytes as TB/GB with one decimal
sub human_size {
    my ($bytes) = @_;
    return '' unless $bytes;
    return $bytes >= 1e12 ? sprintf('%.1f TB', $bytes / 1e12) : sprintf('%.0f GB', $bytes / 1e9);
}

# stock_details: lines of Scrutiny's own message that carry information beyond
# the device fields (e.g. the collector's error text or the missed-upload timing)
sub stock_details {
    return grep { length && !/$STOCK_FIELD/ } split /\n/, ($ENV{SCRUTINY_MESSAGE} // '');
}

# build_message: the enriched text, or dies if the drive can't be looked up
sub build_message {
    my @out = ($TITLE{$TYPE} // "Scrutiny: $TYPE");
    my ($uuid, $details) = find_device();
    my $dev  = $details ? $details->{data}{device} : {};
    my $name = $dev->{friendly_name} || $ENV{SCRUTINY_FRIENDLY_NAME} || '';
    my $path = $ENV{SCRUTINY_DEVICE_NAME} ? "/dev/$ENV{SCRUTINY_DEVICE_NAME}" : '';
    my $head = join " \x{00B7} ", grep { length } $HOST, $name;
    $head .= " ($path)" if length $path;
    push @out, $head if length $head;
    push @out, join " \x{00B7} ", grep { length } $dev->{model_name} // '',
        $ENV{SCRUTINY_DEVICE_SERIAL} // '', human_size($dev->{capacity}), $dev->{device_protocol} // ''
        if $details;

    my @attrs = $details ? failing_attributes($details) : ();
    push @out, '', @attrs if @attrs;
    my @extra = stock_details();
    push @out, '', @extra if @extra;

    if ($details and my $r = $details->{data}{smart_results}[0]) {
        my @facts;
        push @facts, sprintf('Power-on %d h (%.1f y)', $r->{power_on_hours}, $r->{power_on_hours} / 8766)
            if $r->{power_on_hours};
        push @facts, "$r->{temp} \x{00B0}C" if $r->{temp};
        push @facts, 'last data ' . substr($r->{date}, 0, 16) . 'Z' if $r->{date};
        push @out, '', join(" \x{00B7} ", @facts) if @facts;
    }
    push @out, '', ($uuid ? "$DASHBOARD/web/device/$uuid" : "$DASHBOARD/web/dashboard");
    return join "\n", @out;
}

# send_signal: post the text to signal-cli-rest-api's /v2/send
sub send_signal {
    my ($text) = @_;
    if ($ENV{DRY_RUN}) { print "$text\n"; return; }
    my $from = $ENV{SIGNAL_NUMBER}    or die "SIGNAL_NUMBER is not set\n";
    my $to   = $ENV{SIGNAL_RECIPIENT} or die "SIGNAL_RECIPIENT is not set\n";
    my $body = '{"message":' . json_quote($text) . ',"number":' . json_quote($from)
             . ',"recipients":[' . json_quote($to) . ']}';
    open(my $fh, '|-', 'curl', '-fsS', '--max-time', '30', '-o', '/dev/null',
         '-H', 'Content-Type: application/json', '--data-binary', '@-', "$SIGNAL/v2/send")
        or die "cannot run curl: $!\n";
    print $fh $body;
    close($fh) or die 'Signal send failed (curl exit ' . ($? >> 8) . ")\n";
}

my $text = eval { build_message() };
unless (defined $text) {
    my $why = $@ || 'unknown error';
    chomp $why;
    $text = join "\n", grep { defined && length } $ENV{SCRUTINY_SUBJECT}, '',
        $ENV{SCRUTINY_MESSAGE}, '', "(details lookup failed: $why)";
}
utf8::encode($text);
send_signal($text);
