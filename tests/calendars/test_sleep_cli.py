import datetime as dt
import json

from healthmes import __main__ as cli


def test_sleep_reconcile_cli_prints_only_the_redacted_preview(
    settings,
    monkeypatch,
    capsys,
) -> None:
    # Given
    preview = {
        "status": "preview",
        "action": "would_create",
        "calendar": "google",
        "local_date": "2026-07-26",
    }

    async def fake_preview(_settings, target_date):
        assert target_date == dt.date(2026, 7, 26)
        return preview

    monkeypatch.setattr(cli, "_cli_settings", lambda: settings)
    monkeypatch.setattr(cli, "init_engine", lambda _settings: None)
    monkeypatch.setattr(cli, "dispose_engine", lambda: None)
    monkeypatch.setattr(cli, "preview_recent_sleep", fake_preview)

    # When
    result = cli.main(
        ["sleep", "reconcile", "--dry-run", "--date", "2026-07-26"]
    )

    # Then
    assert result == 0
    assert json.loads(capsys.readouterr().out) == preview


def test_sleep_recheck_cli_prints_a_read_only_diagnostic(settings, monkeypatch, capsys) -> None:
    result_payload = {
        "status": "read_only_recheck",
        "outcome": "no_provider_record",
        "calendar_write": "unchanged",
    }

    async def fake_recheck(_settings, night_start):
        assert night_start == dt.date(2026, 7, 26)
        return result_payload

    monkeypatch.setattr(cli, "_cli_settings", lambda: settings)
    monkeypatch.setattr(cli, "recheck_sleep_night", fake_recheck)

    assert cli.main(["sleep", "recheck", "--dry-run", "--date", "2026-07-26"]) == 0
    assert json.loads(capsys.readouterr().out) == result_payload
