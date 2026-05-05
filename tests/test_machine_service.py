from datetime import timedelta

from app.db.models import utcnow
from app.services.machine_service import seconds_until


def test_seconds_until_future():
    assert 8 <= seconds_until(utcnow() + timedelta(seconds=10)) <= 10


def test_seconds_until_past_is_zero():
    assert seconds_until(utcnow() - timedelta(seconds=10)) == 0


def test_seconds_until_none():
    assert seconds_until(None) is None
