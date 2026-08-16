from datetime import UTC, datetime, timedelta


def test_legacy_fixture_clock_keeps_august_media_within_retention(legacy_fixture_clock):
    captured_at = datetime(2026, 8, 5, 23, 30, tzinfo=UTC)

    assert datetime.now(UTC) - captured_at < timedelta(days=7)
