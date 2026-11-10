"""Parent-owned, read-only managed-main delegation projections."""
from typing import Literal

from .base import Params, Payload, Result
from .registry import canonical_method, event


class PanelSubagentPageParams(Params):
    before: int | None = None
    limit: int | None = None


class PanelSubagentHistoryParams(PanelSubagentPageParams):
    subagent_id: str


class PanelSubagentHistoryPage(Result):
    before: int
    has_more: bool


class PanelSubagentListParams(Params):
    before: str | None = None
    limit: int | None = None
    tracked: list[str] | None = None


class PanelSubagentListPage(Result):
    before: str
    has_more: bool


class PanelPersistentSubagent(Result):
    subagent_id: str
    name: str
    goal: str
    status: str
    last_status: str | None
    summary: str
    updated_at: float


class PanelSubagentListResult(Result):
    subagents: list[PanelPersistentSubagent]
    tracked: list['PanelSubagentState']
    page: PanelSubagentListPage


class PanelSubagentState(Result):
    subagent_id: str
    status: str
    last_status: str | None
    updated_at: float


class PanelChildDisplayRow(Result):
    row_id: int
    display_id: str
    role: Literal['tool', 'assistant']
    text: str
    truncated: bool
    name: str | None = None
    tool_call_id: str | None = None
    status: Literal['started', 'returned'] | None = None
    timestamp: float | None = None


class PanelSubagentHistoryResult(Result):
    subagent_id: str
    messages: list[PanelChildDisplayRow]
    page: PanelSubagentHistoryPage


class PanelSubagentsChanged(Payload):
    pass


canonical_method('panel.subagents.list', params=PanelSubagentListParams, result=PanelSubagentListResult)
canonical_method('panel.subagents.history', params=PanelSubagentHistoryParams, result=PanelSubagentHistoryResult)
event('panel.subagents.changed', payload=PanelSubagentsChanged)
