"""Retention boundary for the legacy activity read model."""

from datetime import UTC, datetime, timedelta

from healthmes.engine.cognitive_energy import load_store_day_context
from healthmes.storage import update_retention_policy
from healthmes.store import AppUsageSample


def _usage(device_id: str, bucket_start: datetime) -> AppUsageSample:
    return AppUsageSample(
        device_id=device_id,
        bucket_start=bucket_start,
        app_package="com.example.editor",
        foreground_seconds=600,
        launches=4,
        category="productivity",
    )


def test_cognitive_energy_hides_expired_legacy_usage_before_maintenance(
    session,
) -> None:
    update_retention_policy(session, "activity_raw", "1d")
    now = datetime.now(UTC)
    expired = (now - timedelta(days=2)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    retained = (now - timedelta(hours=1)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    session.add_all(
        [
            _usage("expired", expired),
            _usage("retained", retained),
        ]
    )
    session.flush()

    expired_context = load_store_day_context(session, expired.date())
    retained_context = load_store_day_context(session, retained.date())

    assert expired_context.usage == ()
    assert [row.app_package for row in retained_context.usage] == [
        "com.example.editor"
    ]
