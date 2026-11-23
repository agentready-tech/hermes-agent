"""Parent-owned managed-main delegation and Project Goal contracts."""
from typing import Literal

from pydantic import Field

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


class PanelAssignmentPageParams(Params):
    before: str | None = None
    limit: int | None = None


class PanelAssignmentPage(Result):
    before: str | None
    has_more: bool


class PanelAssignment(Result):
    project_id: str
    title: str
    status: str
    stage: str
    created_at: float


class PanelAssignmentDetail(PanelAssignment):
    description: str
    verification: str
    constraints: str
    next_step: str
    blocker: str
    unblock: str
    source: str | None
    next_check_at: str | None


class PanelAssignmentList(Result):
    goals: list[PanelAssignment]
    page: PanelAssignmentPage


class PanelAssignmentGraphParams(PanelAssignmentPageParams):
    project_id: str | None = None
    parent_id: str | None = None


class PanelAssignmentNodeKey(Params):
    id: str
    parent_id: str


class PanelAssignmentNode(Result):
    id: str
    parent_id: str
    time: float
    kind: Literal['project', 'session']
    name: str
    goal: str
    status: str
    child_page: PanelAssignmentPage | None = None


class PanelAssignmentGraph(Result):
    goal: PanelAssignmentDetail | None
    nodes: list[PanelAssignmentNode]
    page: PanelAssignmentPage


class PanelAssignmentSelection(Params):
    id: str
    type: Literal['node', 'edge']


class PanelAssignmentTimelineParams(PanelAssignmentPageParams):
    project_id: str | None = None
    node_ids: list[PanelAssignmentNodeKey]
    tools: bool = False
    selection: PanelAssignmentSelection | None = None


class PanelAssignmentEvent(Result):
    id: str
    time: float
    kind: Literal['goal', 'task', 'message', 'result', 'tool']
    from_: str = Field(alias='from')
    to: str
    from_name: str | None = None
    to_name: str | None = None
    status: str | None = None
    tool: str | None = None
    text: str


class PanelAssignmentTimeline(Result):
    events: list[PanelAssignmentEvent]
    page: PanelAssignmentPage


class PanelAssignmentControlParams(Params):
    project_id: str
    action: Literal['pause', 'resume', 'cancel']


class PanelAssignmentControlResult(Result):
    goal: PanelAssignmentDetail


canonical_method('panel.subagents.list', params=PanelSubagentListParams, result=PanelSubagentListResult)
canonical_method('panel.subagents.history', params=PanelSubagentHistoryParams, result=PanelSubagentHistoryResult)
canonical_method('panel.subagents.assignments', params=PanelAssignmentPageParams, result=PanelAssignmentList)
canonical_method('panel.subagents.graph', params=PanelAssignmentGraphParams, result=PanelAssignmentGraph)
canonical_method('panel.subagents.timeline', params=PanelAssignmentTimelineParams, result=PanelAssignmentTimeline)
canonical_method('panel.subagents.control', params=PanelAssignmentControlParams, result=PanelAssignmentControlResult)
event('panel.subagents.changed', payload=PanelSubagentsChanged)
