"""ActivityWatch periodic-import job contracts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from healthmes.activity import jobs as jobs_module
from healthmes.activity.contracts import (
    ActivityBatchOut,
    ActivityCapability,
    ActivityCollectionStatusUpdate,
    ActivityCollectionUpdate,
    ActivityPermissionStatus,
    ActivityPlatform,
)
from healthmes.activity.jobs import build_activitywatch_job
from healthmes.activity.repository import (
    get_control_payload,
    update_collection_config,
    update_collection_status,
    update_cursor,
)

NOW = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)


def _session_factory(session: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=session.get_bind(),
        autocommit=False,
        autoflush=False,
    )


def _enabled_settings(settings, **updates):
    return settings.model_copy(
        update={
            "activitywatch_enabled": True,
            "activitywatch_device_id": "macbook-primary",
            "activitywatch_platform": "macos",
            "activitywatch_timezone": "Asia/Seoul",
            "activitywatch_base_url": "http://127.0.0.1:5600",
            "activitywatch_window_minutes": 180,
            "activitywatch_timeout_seconds": 7.5,
            **updates,
        }
    )


def _empty_result():
    return SimpleNamespace(
        response=ActivityBatchOut(
            accepted=0,
            created=0,
            updated=0,
            duplicates=0,
            excluded=0,
            affected_dates=[],
        )
    )


def test_disabled_activitywatch_does_not_build_job(settings) -> None:
    assert build_activitywatch_job(settings) is None


def test_first_run_uses_bounded_window_then_persisted_cursor(
    settings,
    session,
    monkeypatch,
) -> None:
    configured = _enabled_settings(settings)
    factory = _session_factory(session)
    requests = []

    def fake_import(db, request, *, client, now):
        requests.append(request)
        update_cursor(
            db,
            request.device_id,
            "activitywatch:window",
            now.isoformat(),
            platform=request.platform,
            now=now,
        )
        return _empty_result()

    monkeypatch.setattr(jobs_module, "import_activitywatch", fake_import)
    job = build_activitywatch_job(
        configured,
        client_factory=lambda: object(),
        session_factory=factory,
        now_provider=lambda: NOW,
    )

    assert job is not None
    assert job() == _empty_result().response
    assert job() == _empty_result().response
    assert requests[0].start_at == NOW - timedelta(minutes=180)
    assert requests[0].end_at == NOW
    assert requests[1].start_at is None
    assert requests[1].end_at is None
    assert requests[1].timezone == "Asia/Seoul"
    assert requests[1].platform is ActivityPlatform.MACOS


def test_default_client_receives_configured_loopback_url_and_timeout(
    settings,
    session,
    monkeypatch,
) -> None:
    configured = _enabled_settings(settings)
    captured = {}

    class FakeClient:
        def __init__(self, base_url, *, timeout):
            captured["base_url"] = base_url
            captured["timeout"] = timeout

    def fake_import(db, request, *, client, now):
        captured["client"] = client
        return _empty_result()

    monkeypatch.setattr(jobs_module, "ActivityWatchClient", FakeClient)
    monkeypatch.setattr(jobs_module, "import_activitywatch", fake_import)
    job = build_activitywatch_job(
        configured,
        session_factory=_session_factory(session),
        now_provider=lambda: NOW,
    )

    assert job is not None
    assert job() is not None
    assert isinstance(captured["client"], FakeClient)
    assert captured["base_url"] == "http://127.0.0.1:5600"
    assert captured["timeout"] == 7.5


@pytest.mark.parametrize("boundary", ("disabled", "revoked"))
def test_privacy_boundary_blocks_before_activitywatch_http_read(
    settings,
    session,
    boundary,
) -> None:
    configured = _enabled_settings(settings)
    factory = _session_factory(session)
    with factory.begin() as db:
        if boundary == "disabled":
            update_collection_config(
                db,
                configured.activitywatch_device_id,
                ActivityCollectionUpdate(
                    platform=ActivityPlatform.MACOS,
                    enabled=False,
                ),
                now=NOW,
            )
        else:
            update_collection_status(
                db,
                configured.activitywatch_device_id,
                ActivityCollectionStatusUpdate(
                    platform=ActivityPlatform.MACOS,
                    capability=ActivityCapability.DETAILED,
                    permission_status=ActivityPermissionStatus.REVOKED,
                    status_observed_at=NOW,
                ),
                now=NOW,
            )

    class MustNotReadClient:
        def list_buckets(self):
            raise AssertionError("privacy gate must run before ActivityWatch HTTP")

    job = build_activitywatch_job(
        configured,
        client_factory=MustNotReadClient,
        session_factory=factory,
        now_provider=lambda: NOW,
    )

    assert job is not None
    assert job() is None
    with factory() as db:
        state = get_control_payload(
            db,
            configured.activitywatch_device_id,
            platform=ActivityPlatform.MACOS,
        )
    assert state["cursors"] == {}
    assert state["last_uploaded_at"] is None


def test_http_timeout_is_contained_for_next_scheduler_retry(
    settings,
    session,
    caplog,
) -> None:
    configured = _enabled_settings(settings)

    class TimingOutClient:
        def list_buckets(self):
            raise httpx.ReadTimeout("ActivityWatch did not answer")

    job = build_activitywatch_job(
        configured,
        client_factory=TimingOutClient,
        session_factory=_session_factory(session),
        now_provider=lambda: NOW,
    )

    assert job is not None
    with caplog.at_level(logging.WARNING, logger=jobs_module.__name__):
        assert job() is None
    assert "next interval will retry" in caplog.text


def test_unexpected_runtime_failure_is_contained(
    settings,
    session,
    caplog,
) -> None:
    configured = _enabled_settings(settings)
    job = build_activitywatch_job(
        configured,
        session_factory=_session_factory(session),
        now_provider=lambda: datetime(2026, 8, 12, 3, 0),
    )

    assert job is not None
    with caplog.at_level(logging.ERROR, logger=jobs_module.__name__):
        assert job() is None
    assert "next interval will retry" in caplog.text


def test_runtime_disable_skips_job_without_opening_client(
    settings,
    session,
) -> None:
    configured = _enabled_settings(settings)
    job = build_activitywatch_job(
        configured,
        client_factory=lambda: pytest.fail("disabled job opened a client"),
        session_factory=_session_factory(session),
        now_provider=lambda: NOW,
    )
    configured.activitywatch_enabled = False

    assert job is not None
    assert job() is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("activitywatch_base_url", "http://activitywatch.example:5600"),
        ("activitywatch_base_url", "https://127.0.0.1:5600"),
        ("activitywatch_window_minutes", 7 * 24 * 60 + 1),
        ("activitywatch_timeout_seconds", 0),
        ("activitywatch_timezone", "Not/A-Timezone"),
        ("activitywatch_window_bucket_id", ".."),
    ),
)
def test_activitywatch_settings_fail_closed(field, value) -> None:
    with pytest.raises(ValueError):
        jobs_module.Settings(_env_file=None, **{field: value})
