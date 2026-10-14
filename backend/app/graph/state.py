"""Shared contracts: the EventState that flows through the LangGraph graph, and its models.

Do not change these shapes without telling the team (see CLAUDE.md, "Shared contracts").
"""

import operator
from datetime import UTC, datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class Brand(BaseModel):
    id: str
    name: str
    category: str
    address: str
    lat: float
    lng: float
    distance_km: float
    contact_email: str | None = None
    website: str | None = None
    fit_score: float = 0.0
    fit_reason: str = ""
    unreachable: bool = False  # set by arbitration A1 when an email to this brand bounced


class Tier(BaseModel):
    name: Literal["Bronze", "Silver", "Gold"]
    price_inr: int
    deliverables: list[str]
    expected_reach: int


class Proposal(BaseModel):
    brand_id: str
    version: int
    tiers: list[Tier]
    pitch: str
    pdf_url: str | None = None
    # draft -> (needs_revision) -> ready -> sent -> accepted | declined; also on_hold, infeasible
    status: str = "draft"
    reach_note: str | None = None  # L8: "estimate calibrated from N past events" when calibration was applied


ThreadStatus = Literal["draft", "sent", "replied", "nudged", "accepted", "declined", "pushback", "bounced"]


class OutreachThread(BaseModel):
    brand_id: str
    thread_id: str | None = None
    status: ThreadStatus = "draft"
    last_reply_class: str | None = None
    history: list[dict] = Field(default_factory=list)


MessageType = Literal["request", "result", "critique", "revision_request", "replan", "escalation", "decision"]


class AgentMessage(BaseModel):
    id: str
    from_agent: str
    to_agent: str
    type: MessageType
    content: dict
    reasoning: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EventState(TypedDict, total=False):
    event_id: str
    event: dict  # name, college, date, expected_footfall, audience, budget_goal_inr, lat, lng
    brands: list[Brand]
    selected_brand_ids: list[str]
    proposals: dict[str, Proposal]
    outreach: dict[str, OutreachThread]
    evidence: list[dict]
    roi_report: dict | None
    messages: Annotated[list[AgentMessage], operator.add]  # the inter-agent message bus (blackboard)
    pending_approval: dict | None
    phase: str
    step_count: int
    errors: list[dict]
    # supervisor's own record: every route, guardrail hit and arbitration, with its reason and source
    decision_log: list[dict]
    feedback: dict[str, list[str]]  # brand_id -> outreach outcomes (accepted, declined, bounced, silent)
    revision_count: dict[str, int]  # brand_id -> agent-initiated revision requests let through (cap 2)
    publish_with_gaps: bool  # organizer explicitly chose "Publish with gaps disclosed" (arbitration A3)
    target_brand_count: int  # shortlist size the organizer approved; L6 tops up to keep it
    actual_footfall: int | None  # optional, given with the audit request; used for L8 calibration


# Classes the checkpointer may deserialize (LangGraph warns about unlisted types).
CHECKPOINT_TYPES = [(__name__, cls.__name__) for cls in (Brand, Tier, Proposal, OutreachThread, AgentMessage)]


def initial_state(event_id: str, event: dict) -> EventState:
    return EventState(
        event_id=event_id,
        event=event,
        brands=[],
        selected_brand_ids=[],
        proposals={},
        outreach={},
        evidence=[],
        roi_report=None,
        messages=[],
        pending_approval=None,
        phase="discovery",
        step_count=0,
        errors=[],
        decision_log=[],
        feedback={},
        revision_count={},
        publish_with_gaps=False,
        target_brand_count=0,
        actual_footfall=None,
    )
