import pathlib

import pytest

from winegard2mqtt.nmea import NmeaAccumulator, checksum, split_sentence

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def epoch():
    """One epoch from the live stream, captured 2026-10-12.

    Coordinates and satellite azimuth/elevation are substituted, with
    checksums recomputed; sentence order and formatting are verbatim.
    """
    return (FIXTURES / "nmea_fix.txt").read_text().splitlines()


def accumulate(lines):
    accumulator = NmeaAccumulator()
    for line in lines:
        accumulator.feed(line)
    return accumulator


def resign(sentence: str) -> str:
    """Recompute the checksum after editing a sentence's fields."""
    body = sentence[1:sentence.index("*")]
    return f"${body}*{checksum(body)}"


def edit(lines, kind, index, value):
    """Replace one field of the ``kind`` sentence, keeping it validly signed."""
    out = []
    for line in lines:
        if line[3:6] == kind:
            fields = line[1:line.index("*")].split(",")
            fields[index] = value
            line = resign("$" + ",".join(fields) + "*")
        out.append(line)
    return out


# --- sentence validation ----------------------------------------------------


def test_a_valid_sentence_splits_into_fields(epoch):
    fields = split_sentence(epoch[0])
    assert fields[0] == "GPGGA"
    assert fields[1] == "203628.00"


def test_a_bad_checksum_is_rejected(epoch):
    body = epoch[0][:epoch[0].index("*")]
    assert split_sentence(body + "*00") is None


def test_a_truncated_sentence_is_rejected(epoch):
    """A reconnect can cut a sentence mid-line; it must not half-parse."""
    assert split_sentence(epoch[0][:30]) is None


def test_lowercase_checksum_hex_is_accepted(epoch):
    assert split_sentence(epoch[0][:-2] + epoch[0][-2:].lower()) is not None


# --- assembling a fix -------------------------------------------------------


def test_a_real_epoch_yields_a_position(epoch):
    fix = accumulate(epoch).fix()
    assert fix is not None
    assert fix.latitude == pytest.approx(12.34567)
    assert fix.longitude == pytest.approx(-65.43210)


def test_quality_fields_come_from_gga_and_gsa(epoch):
    fix = accumulate(epoch).fix()
    assert fix.fix_type == "3D"
    assert fix.satellites == 9
    assert fix.hdop == pytest.approx(0.7)
    assert fix.gps_accuracy == 4
    assert fix.altitude == pytest.approx(84.8)


def test_speed_is_km_per_hour_and_heading_is_true(epoch):
    """Same units as the JSON path, so the HA sensors need no change."""
    epoch = edit(epoch, "VTG", 7, "42.5")
    fix = accumulate(epoch).fix()
    assert fix.speed == pytest.approx(42.5)
    assert fix.heading == pytest.approx(348.8)


def test_speed_falls_back_to_rmc_knots_without_vtg(epoch):
    epoch = [line for line in edit(epoch, "RMC", 7, "10.0") if "VTG" not in line]
    assert accumulate(epoch).fix().speed == pytest.approx(18.5)


def test_utc_comes_from_the_sentence_not_the_arrival_time(epoch):
    """The router repeats cached epochs; their age must stay visible."""
    assert accumulate(epoch).fix().utc == "2026-10-12T20:36:28+00:00"


def test_southern_and_eastern_hemispheres_are_negative_and_positive(epoch):
    epoch = edit(edit(epoch, "RMC", 4, "S"), "RMC", 6, "E")
    fix = accumulate(epoch).fix()
    assert fix.latitude == pytest.approx(-12.34567)
    assert fix.longitude == pytest.approx(65.43210)


def test_other_talker_ids_are_accepted(epoch):
    """A multi-constellation receiver reports as GN rather than GP."""
    gn = [resign(line.replace("$GP", "$GN", 1)) for line in epoch]
    assert accumulate(gn).fix() is not None


def test_a_missing_gsa_still_yields_a_position(epoch):
    fix = accumulate([line for line in epoch if "GSA" not in line]).fix()
    assert fix is not None
    assert fix.fix_type is None


# --- rejecting non-fixes ----------------------------------------------------


def test_rmc_void_status_is_not_a_fix(epoch):
    assert accumulate(edit(epoch, "RMC", 2, "V")).fix() is None


def test_gga_quality_zero_is_not_a_fix(epoch):
    assert accumulate(edit(epoch, "GGA", 6, "0")).fix() is None


def test_gga_and_rmc_from_different_epochs_are_not_combined(epoch):
    """After a reconnect one sentence may be an epoch ahead of the other."""
    assert accumulate(edit(epoch, "GGA", 1, "203629.00")).fix() is None


def test_an_epoch_is_incomplete_until_its_rmc_arrives(epoch):
    """Live order is GGA, VTG, RMC: a new GGA briefly disagrees with the old
    RMC, and that must read as "wait", not as "fix lost"."""
    accumulator = accumulate(epoch)
    assert accumulator.complete
    next_gga = edit(epoch, "GGA", 1, "203629.00")[0]
    accumulator.feed(next_gga)
    assert not accumulator.complete
    accumulator.feed(edit(epoch, "RMC", 1, "203629.00")[2])
    assert accumulator.complete
    assert accumulator.fix() is not None


def test_no_rmc_means_no_fix(epoch):
    assert accumulate([line for line in epoch if "RMC" not in line]).fix() is None


def test_nothing_received_means_no_fix():
    assert NmeaAccumulator().fix() is None


def test_minutes_of_60_or_more_are_not_a_coordinate(epoch):
    assert accumulate(edit(epoch, "RMC", 3, "1260.000000")).fix() is None


def test_empty_coordinates_are_not_a_fix(epoch):
    epoch = edit(edit(epoch, "RMC", 3, ""), "RMC", 4, "")
    assert accumulate(epoch).fix() is None


def test_gsv_and_garbage_are_ignored(epoch):
    accumulator = NmeaAccumulator()
    assert accumulator.feed(epoch[-1]) is False  # GSV
    assert accumulator.feed("not nmea") is False
    assert accumulator.feed("") is False
