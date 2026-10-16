"""The demo fixture world: businesses, their scripted inbox replies, seeded past deals and evidence photos.

Only the *world* is scripted. The agents and the supervisor still reason with the real LLM (in "demo"
mode) and decide everything themselves; the loops fire because the world reliably presents the situations
that trigger them (rules in code), not because any agent decision is faked:

- only 4 businesses lie within 2 km           -> L1 Discovery self-replan (fewer than 5 good fits)
- Metro Wholesale Traders is a wholesaler      -> L7 Proposal finds it infeasible, Discovery replaces it
- Chai Junction Cafe is closest, but bounces   -> L4 replacement, A1 bounce beats ranking (if ranked top 3)
- two gyms decline                             -> L4 twice, then L6 category feedback (gym x0.6)
- Bean Theory Cafe replies "Rs 8,000 max"      -> L3 pushback revision, then accepts Silver
- Apex Coaching: 3 seeded coaching deals       -> A2 caps the price at 1.25 x median
- cafes have only 2 seeded deals               -> A2 logs "insufficient data"
- demo evidence leaves one deliverable out     -> L5 escalation and A3 blocks publishing
No real email is sent and no map service is called. All addresses use the reserved ``.invalid`` domain.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from app.graph.state import Brand

PREFIX = "demo-"

# The demo event (same as docs/sample_input.json) and its campus location.
EVENT = {
    "name": "Fergusson Fest 2026", "college": "Fergusson College, Pune", "event_date": "2026-12-12",
    "expected_footfall": 1800,
    "audience": "18-22 year old arts, science and commerce students; many live in nearby PGs and hostels",
    "budget_goal_inr": 120000, "lat": 18.5236, "lng": 73.8394,
}

# (slug, name, category, distance_km, rehearsal_fit) - rehearsal_fit is used ONLY by the offline rehearsal LLM.
BUSINESSES = [
    ("chai-junction", "Chai Junction Cafe", "cafe", 0.3, 0.95),
    ("bean-theory", "Bean Theory Cafe", "cafe", 0.6, 0.9),
    ("ironcore-gym", "IronCore Gym", "gym", 0.9, 0.85),
    ("metro-wholesale", "Metro Wholesale Traders", "wholesale", 1.4, 0.65),
    ("pulsefit-gym", "PulseFit Gym", "gym", 2.4, 0.8),
    ("apex-coaching", "Apex Coaching Classes", "coaching", 2.8, 0.85),
    ("crumbs-bakery", "Crumbs & Co Bakery", "bakery", 3.1, 0.75),
    ("glow-salon", "Glow Studio Salon", "salon", 3.4, 0.7),
    ("page-turner", "Page Turner Books", "bookstore", 3.6, 0.68),
    ("campus-stationers", "Campus Stationers", "stationery", 3.8, 0.66),
]
SUGGESTED_SHORTLIST = [PREFIX + s for s in ("chai-junction", "bean-theory", "ironcore-gym", "metro-wholesale",
                                             "pulsefit-gym", "apex-coaching")]

BOUNCE = "__bounce__"
# Scripted replies: brand slug -> [(after_n_sends, reply_text)]. A brand not listed never replies.
INBOX = {
    "chai-junction": [(1, BOUNCE)],
    "ironcore-gym": [(1, "Thanks for thinking of us, but we are not interested in sponsoring college events this year.")],
    "pulsefit-gym": [(1, "Sorry, not interested - our marketing budget already went to a marathon.")],
    "bean-theory": [(1, "We like the idea, but Rs 8,000 is the max we can spend on this. Could you work with that?"),
                    (2, "Rs 8,000 works for us. We will go with Silver - see you at the fest!")],
    "apex-coaching": [(1, "Good proposal. We will go with Gold, please keep our stall near registration.")],
    "crumbs-bakery": [(1, "Happy to support the students. We will go with Bronze.")],
    "glow-salon": [(1, "Sounds fun! We will go with Bronze.")],
    "campus-stationers": [(1, "Count us in - we will go with Silver.")],
}

# Seeded past accepted deals (owner-level memory for arbitration A2).
SEEDED_DEALS = [("coaching", "Silver", 9000), ("coaching", "Gold", 10000), ("coaching", "Silver", 12000),
                ("cafe", "Silver", 6000), ("cafe", "Gold", 8000)]


def brands() -> list[Brand]:
    return [Brand(id=PREFIX + slug, name=name, category=cat, address="Fergusson College Road area, Pune (demo)",
                  lat=EVENT["lat"], lng=EVENT["lng"], distance_km=km, contact_email=f"hello@{slug}.invalid",
                  website=None) for slug, name, cat, km, _ in BUSINESSES]


def rehearsal_fit(brand_id: str) -> float:
    return next((fit for slug, *_, fit in BUSINESSES if PREFIX + slug == brand_id), 0.3)


async def seed_deals(db, owner_id: str) -> None:
    """Reset this owner's demo deals and seed the past deals, so every demo starts from the same memory."""
    await db.delete_deals(owner_id, PREFIX)
    for i, (category, tier, price) in enumerate(SEEDED_DEALS):
        await db.add_deal(owner_id, {"event_id": None, "brand_id": f"{PREFIX}seed-{category}-{i}",
                                     "category": category, "tier": tier, "price_inr": price})


class DemoPlaces:
    """Fixture map: every demo business within the radius (categories are not filtered, so the
    wholesaler is always found). Never calls Google Places or OpenStreetMap."""

    async def search(self, lat: float, lng: float, radius_km: float, categories: list[str]) -> dict:
        found = [b for b in brands() if b.distance_km <= radius_km]
        return {"source": "demo_fixture", "fallback_reason": "demo mode: fixture businesses, no map call",
                "places": found}

    async def find_contact(self, website: str) -> str | None:
        return None


class DemoInbox:
    """Fixture mailbox with the Gmail tool's interface. Replies depend on how many emails a brand has
    received, which is read from the run state, so the script survives server restarts."""

    SENDER = "paytriq-demo@paytriq.invalid"

    def __init__(self, state: dict) -> None:
        self.sends: dict[str, int] = {
            brand_id: sum(1 for e in thread.history if e.get("type") == "sent")
            for brand_id, thread in state.get("outreach", {}).items()}

    def recipient_for(self, contact_email: str | None, index: int) -> tuple[str, bool]:
        return contact_email or "unknown@paytriq.invalid", True

    def is_own(self, address: str) -> bool:
        return address == self.SENDER

    async def send(self, to, subject, body, attachment=None, thread_id=None, in_reply_to=None) -> dict:
        brand_id = to.split("@")[1].removesuffix(".invalid") if "@" in to else to
        brand_id = PREFIX + brand_id if not brand_id.startswith(PREFIX) else brand_id
        self.sends[brand_id] = self.sends.get(brand_id, 0) + 1
        return {"message_id": f"{brand_id}-sent-{self.sends[brand_id]}", "thread_id": thread_id or f"thread-{brand_id}"}

    async def get_thread(self, thread_id: str) -> list[dict]:
        brand_id = thread_id.removeprefix("thread-")
        out = []
        for i, (after, text) in enumerate(INBOX.get(brand_id.removeprefix(PREFIX), [])):
            if self.sends.get(brand_id, 0) < after:
                continue
            bounce = text == BOUNCE
            out.append({"id": f"{brand_id}-reply-{i}", "subject": "Re: sponsorship",
                        "from": "mailer-daemon@paytriq.invalid" if bounce else f"owner@{brand_id[len(PREFIX):]}.invalid",
                        "text": "Delivery failed: address not found." if bounce else text,
                        "message_id_header": f"<{brand_id}-reply-{i}@paytriq.invalid>",
                        "date_ms": int(datetime.now(UTC).timestamp() * 1000), "is_bounce": bounce})
        return out


def evidence_png(brand_name: str, deliverable: str, event_name: str) -> bytes:
    """A simple generated photo stand-in: a banner/sign showing the brand for one deliverable."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1200, 800), (34, 40, 49))
    draw = ImageDraw.Draw(img)
    draw.rectangle((80, 140, 1120, 620), fill=(245, 245, 240), outline=(200, 160, 40), width=10)
    big, small = ImageFont.load_default(size=72), ImageFont.load_default(size=40)
    draw.text((600, 70), event_name.upper(), font=small, fill=(255, 255, 255), anchor="mm")
    draw.ellipse((140, 250, 380, 490), fill=(79, 70, 229))
    initials = "".join(w[0] for w in brand_name.split()[:2]).upper()
    draw.text((260, 370), initials, font=big, fill=(255, 255, 255), anchor="mm")
    draw.text((760, 330), brand_name, font=big, fill=(20, 20, 20), anchor="mm")
    draw.text((760, 440), "Official sponsor", font=small, fill=(60, 60, 60), anchor="mm")
    draw.text((600, 700), f"Deliverable: {deliverable}", font=small, fill=(255, 255, 255), anchor="mm")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def evidence_plan(sponsors: list[dict]) -> list[tuple[dict, str]]:
    """Which (sponsor, deliverable) pairs get a demo photo: all except the first sponsor's last deliverable,
    which is deliberately missing so the audit has a real gap to report."""
    ordered = sorted(sponsors, key=lambda s: s["brand_id"])
    pairs = [(s, d) for s in ordered for d in s["deliverables"]]
    if ordered and ordered[0]["deliverables"]:
        pairs.remove((ordered[0], ordered[0]["deliverables"][-1]))
    return pairs
