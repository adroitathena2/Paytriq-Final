"""Small builders for test states."""

from app.graph.state import Brand, OutreachThread, Proposal, Tier, initial_state

EVENT = {
    "name": "TechFest 2026", "college": "SIT Pune", "date": "2026-11-20", "expected_footfall": 1500,
    "audience": "18-22 year old engineering students", "budget_goal_inr": 150000,
    "lat": 18.5204, "lng": 73.8567,
}


def brand(i: int, score: float = 0.8, email: str | None = "hello@example.com", category: str = "cafe") -> Brand:
    return Brand(
        id=f"b{i}", name=f"Brand {i}", category=category, address=f"{i} FC Road", lat=18.52, lng=73.85,
        distance_km=0.5 + i / 10, contact_email=email, website=None, fit_score=score, fit_reason="students hang out",
    )


def tiers() -> list[Tier]:
    return [
        Tier(name="Bronze", price_inr=10000, deliverables=["Logo on poster"], expected_reach=600),
        Tier(name="Silver", price_inr=25000, deliverables=["Logo on poster", "Stall"], expected_reach=900),
        Tier(name="Gold", price_inr=50000, deliverables=["Logo on main stage banner", "Stall", "Instagram post"],
             expected_reach=1300),
    ]


def proposal(brand_id: str, status: str = "draft", version: int = 1) -> Proposal:
    return Proposal(brand_id=brand_id, version=version, tiers=tiers(), pitch="A pitch.", pdf_url=None, status=status)


def thread(brand_id: str, status: str = "draft", **kw) -> OutreachThread:
    return OutreachThread(brand_id=brand_id, status=status, **kw)


def state(**overrides):
    s = initial_state("event-1", dict(EVENT))
    s.update(overrides)
    return s
