from pathlib import Path

from healthmes.calendars.google import google_client_secret_path
from healthmes.config import Settings


def resolve_google_client_secret(settings: Settings) -> Path | None:
    standard = google_client_secret_path(settings.data_dir)
    if standard.exists():
        return standard
    override = settings.google_client_secret_file
    if override is not None and Path(override).exists():
        return Path(override)
    packaged = Path(__file__).resolve().parents[2] / "config" / "google_oauth_client.json"
    return packaged if packaged.exists() else None
