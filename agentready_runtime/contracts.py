"""Narrow contracts consumed by the checkpoint policy."""

from typing import Callable, Protocol

TokenEstimator = Callable[[str], int]
SummaryReducer = Callable[[str], str]


class CheckpointArchive(Protocol):
    def save(
        self,
        session_id: str,
        summary: str,
        previous_summary: str | None,
        estimate: TokenEstimator,
    ) -> str:
        """Archive full text and return a retrieval footer; this is not a commit."""
        ...
