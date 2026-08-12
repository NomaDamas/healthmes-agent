"""Public HealthMes decision entrypoint combining reasoning and finalization."""

from __future__ import annotations

import asyncio

from healthmes.decision.agent import HealthMesDecisionAgent
from healthmes.decision.contracts import DecisionRequest, DecisionResult
from healthmes.decision.finalizer import DecisionFinalizer


class HealthMesDecisionEngine:
    """Own the product flow from natural-language request to final record."""

    def __init__(
        self,
        *,
        agent: HealthMesDecisionAgent,
        finalizer: DecisionFinalizer,
    ) -> None:
        self._agent = agent
        self._finalizer = finalizer

    async def ask(self, request: DecisionRequest) -> DecisionResult:
        run = await self._agent.ask(request)
        return await asyncio.to_thread(
            self._finalizer.finalize,
            request,
            run,
        )

    def close(self) -> None:
        self._agent.close()

    async def __aenter__(self) -> HealthMesDecisionEngine:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.close()
