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
