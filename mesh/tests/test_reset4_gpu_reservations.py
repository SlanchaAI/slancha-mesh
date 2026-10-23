"""Synthetic Reservation wire-format and fixed-clock boundary coverage."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from mesh.gpu import reservations
from mesh.gpu.reservations import Reservation


def test_serialization_preserves_offset_precision_and_input():
    payload = {
        "reservation_id": "synthetic-offset",
        "user": "test-user",
        "hostname": "test-host",
        "gb_requested": 1.25,
        "started_at": "2026-05-16T12:00:00.123456+05:30",
        "expires_at": "2026-05-16T12:00:01.654321+05:30",
        "purpose": "synthetic serialization",
        "pid": 12345,
    }
    original = payload.copy()
    reservation = Reservation.from_json(payload)

    assert payload == original
    assert reservation.started_at == datetime(
        2026, 5, 16, 12, 0, 0, 123456,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    assert reservation.expires_at - reservation.started_at == timedelta(
        seconds=1, microseconds=530865,
    )
    assert json.loads(json.dumps(reservation.to_json())) == original


def test_deserialization_defaults_missing_optional_fields():
    payload = {
        "reservation_id": "synthetic-defaults",
        "user": "test-user",
        "hostname": "test-host",
        "gb_requested": 2.5,
        "started_at": "2026-05-16T12:00:00+00:00",
        "expires_at": "2026-05-16T12:01:00+00:00",
    }
    reservation = Reservation.from_json(payload)

    assert reservation.purpose == ""
    assert reservation.pid is None
    assert reservation.to_json() == {**payload, "purpose": "", "pid": None}
    assert "purpose" not in payload
    assert "pid" not in payload


@pytest.mark.parametrize(
    "offset_us, expired, remaining",
    [(1, False, 0.000001), (0, True, 0.0), (-1, True, 0.0)],
    ids=["one-microsecond-before", "exact-expiry", "one-microsecond-after"],
)
def test_expiry_boundary(monkeypatch, offset_us, expired, remaining):
    now = datetime(2026, 5, 16, 12, tzinfo=timezone.utc)
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(reservations, "datetime", clock)
    reservation = Reservation(
        reservation_id="synthetic-boundary", user="test-user",
        hostname="test-host", gb_requested=1.0,
        started_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(microseconds=offset_us),
    )

    assert reservation.is_expired is expired
    assert reservation.remaining_s == remaining
    assert clock.now.call_count == 2
    clock.now.assert_called_with(timezone.utc)
