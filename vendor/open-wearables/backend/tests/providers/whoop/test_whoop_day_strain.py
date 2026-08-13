from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.constants.health_scores import HEALTH_SCORE_RANGES, ScoreRange
from app.schemas.enums import HealthScoreCategory, ProviderName
from app.services.providers.whoop.data_247 import Whoop247Data
from app.services.providers.whoop.strategy import WhoopStrategy


@pytest.fixture
def data_247() -> Whoop247Data:
    return WhoopStrategy().data_247


@pytest.fixture
def scored_cycle() -> dict:
    return {
        "id": 93845,
        "start": "2026-08-13T01:30:00Z",
        "updated_at": "2026-08-13T06:20:00Z",
        "score_state": "SCORED",
        "score": {"strain": 14.2},
    }


class TestWhoopDayStrain:
    def test_declares_the_same_whoop_scale_as_strain(self) -> None:
        assert HEALTH_SCORE_RANGES[HealthScoreCategory.DAY_STRAIN][ProviderName.WHOOP] == ScoreRange(0, 21)

    def test_normalizes_cycle_strain_separately_from_workout_strain(
        self, data_247: Whoop247Data, scored_cycle: dict
    ) -> None:
        user_id = uuid4()

        score = data_247.normalize_day_strain_health_score(scored_cycle, user_id)

        assert score is not None
        assert score.category == HealthScoreCategory.DAY_STRAIN
        assert score.value == 14.2
        assert score.recorded_at == datetime(2026, 8, 13, 6, 20, tzinfo=timezone.utc)
        assert score.components is not None
        assert score.components["cycle_id"].qualifier == "93845"

    def test_rejects_unscored_or_unparseable_cycles(self, data_247: Whoop247Data, scored_cycle: dict) -> None:
        user_id = uuid4()
        scored_cycle["score_state"] = "PENDING_SCORE"
        assert data_247.normalize_day_strain_health_score(scored_cycle, user_id) is None

        scored_cycle["score_state"] = "SCORED"
        scored_cycle["updated_at"] = "not-a-timestamp"
        assert data_247.normalize_day_strain_health_score(scored_cycle, user_id) is None

    @patch("app.services.providers.whoop.data_247.health_score_service")
    def test_persists_cycle_scores_with_their_source_update_time(
        self, health_score_service: MagicMock, data_247: Whoop247Data, scored_cycle: dict
    ) -> None:
        db = MagicMock()
        with patch.object(data_247, "get_cycle_data", return_value=[scored_cycle]):
            count = data_247.load_and_save_day_strain(
                db,
                uuid4(),
                datetime(2026, 8, 12, tzinfo=timezone.utc),
                datetime(2026, 8, 13, tzinfo=timezone.utc),
            )

        assert count == 1
        health_score_service.bulk_create.assert_called_once()
        persisted = health_score_service.bulk_create.call_args.args[1][0]
        assert persisted.category == HealthScoreCategory.DAY_STRAIN
        assert persisted.recorded_at == datetime(2026, 8, 13, 6, 20, tzinfo=timezone.utc)
        db.commit.assert_called_once()
