"""Fake tools and scripted LLM policies. Policies read the context the agent passes to the LLM,
so they behave like a (very simple) model that reacts to observations."""

from __future__ import annotations

from collections.abc import Callable

from app.graph.state import Brand
from app.tools.base import ToolError


def call(name: str, reasoning: str = "scripted", **args) -> dict:
    return {"name": name, "args": args, "reasoning": reasoning}


def tools_used(ctx: dict) -> list[str]:
    return [o["tool"] for o in ctx.get("observations", [])]


def make_brand(name: str, category: str = "cafe", email: str | None = "owner@shop.in", km: float = 1.0) -> Brand:
    return Brand(id=name.lower().replace(" ", "-"), name=name, category=category, address="FC Road", lat=18.52,
                 lng=73.85, distance_km=km, contact_email=email, website=None)


class FakePlaces:
    """``results(radius_km, categories)`` returns brands or raises; every call is recorded."""

    def __init__(self, results: Callable[[float, list[str]], list[Brand]], source: str = "openstreetmap"):
        self.results = results
        self.source = source
        self.calls: list[dict] = []

    async def search(self, lat: float, lng: float, radius_km: float, categories: list[str]) -> dict:
        self.calls.append({"radius_km": radius_km, "categories": list(categories)})
        places = self.results(radius_km, categories)
        return {"source": self.source, "fallback_reason": "GOOGLE_MAPS_API_KEY not set", "places": places}

    async def find_contact(self, website: str) -> str | None:
        return "found@" + website


def failing(times: int, then: Callable[[float, list[str]], list[Brand]]):
    """Places results that raise ``times`` times before working."""
    state = {"n": 0}

    def results(radius, categories):
        state["n"] += 1
        if state["n"] <= times:
            raise ToolError("Overpass unavailable: 504 Gateway Timeout")
        return then(radius, categories)

    return results


# --- Discovery -----------------------------------------------------------------------------------

def discovery_plan(ctx: dict) -> dict:
    """Search (following any replan), adapt once after a failed search, score, finish."""
    obs = ctx["observations"]
    used = tools_used(ctx)
    replan = ctx.get("replan")
    if not used:
        if replan:
            return call("search_places", categories=replan["categories"], radius_km=replan["radius_km"],
                        reasoning="following the replan")
        return call("search_places", categories=["cafe", "gym"], radius_km=2, reasoning="students like cafes and gyms")
    if not obs[-1]["result"]["ok"] and obs[-1]["tool"] == "search_places":
        if used.count("search_places") < 2:
            return call("search_places", categories=["bookstore"], radius_km=2, reasoning="search failed, try another")
        return call("finish", summary="map services keep failing")
    if "score_brands" not in used:
        return call("score_brands", reasoning="rank the new candidates")
    return call("finish", summary="ranked the candidates")


def score_by_name(ctx: dict) -> dict:
    """Brands with 'Good' in their name are a good fit."""
    return {"scores": [{"id": b["id"], "fit_score": 0.85 if "Good" in b["name"] else 0.3,
                        "fit_reason": "fits students" if "Good" in b["name"] else "weak overlap"}
                       for b in ctx["brands"]]}


def discovery_replace(ctx: dict) -> dict:
    used = tools_used(ctx)
    if "list_alternatives" not in used:
        return call("list_alternatives", reasoning="see unused options")
    if "pick_replacement" not in used:
        best = ctx["observations"][-1]["result"]["options"][0]
        return call("pick_replacement", brand_id=best["id"], why="next best fit with an email")
    return call("finish", summary="replacement chosen")


# --- Proposal ------------------------------------------------------------------------------------

def proposal_plan(ctx: dict) -> dict:
    """Draft, check pricing (redraft once on warnings), render, finish."""
    obs = ctx["observations"]
    used = tools_used(ctx)
    if not used:
        return call("draft_proposal", guidance="", reasoning="start with a draft")
    last = obs[-1]
    if last["tool"] == "draft_proposal":
        return call("check_pricing", reasoning="verify prices") if last["result"]["ok"] else call(
            "finish", summary="could not draft")
    if last["tool"] == "check_pricing" and not last["result"]["ok_to_render"] and used.count("draft_proposal") < 2:
        return call("draft_proposal", guidance="; ".join(last["result"]["warnings"]), reasoning="fix pricing warnings")
    if "render_pdf" not in used:
        return call("render_pdf", reasoning="draft is acceptable")
    return call("finish", summary="proposal ready")


def proposal_write(ctx: dict) -> dict:
    """Writes a draft; honours max_price_inr when given, otherwise prices from the typical budget."""
    gold = ctx.get("max_price_inr") or ctx["typical_budget_inr"]
    return {
        "pitch": f"{ctx['brand']['name']} meets {ctx['event']['expected_footfall']} students.",
        "tiers": [
            {"name": "Bronze", "price_inr": int(gold * 0.3), "deliverables": ["Logo on posters"]},
            {"name": "Silver", "price_inr": int(gold * 0.6), "deliverables": ["Logo on posters", "Stall"]},
            {"name": "Gold", "price_inr": gold,
             "deliverables": ["Logo on main stage banner", "Stall", "1 Instagram post"]},
        ],
        "changes_made": f"v{ctx['version']}: " + (ctx.get("revision_constraint") or "first draft"),
    }


class FakePdf:
    def __init__(self) -> None:
        self.rendered: list[tuple[str, int]] = []

    async def render_and_store(self, run_id, event, brand, proposal) -> str:
        self.rendered.append((brand.id, proposal.version))
        return f"https://files.test/{brand.id}-v{proposal.version}.pdf"


# --- Outreach ------------------------------------------------------------------------------------

def _last_result(ctx: dict, tool: str) -> dict | None:
    return next((o["result"] for o in reversed(ctx.get("observations", [])) if o["tool"] == tool), None)


def outreach_plan(ctx: dict) -> dict:
    """Critique -> revise/prepare; send if approved; poll -> classify -> act per label."""
    used = tools_used(ctx)
    if ctx["drafts_to_review"]:
        if "critique_proposals" not in used:
            return call("critique_proposals", brand_ids=ctx["drafts_to_review"], reasoning="review before sending")
        verdicts = _last_result(ctx, "critique_proposals")["verdicts"]
        requested = {o["args"]["brand_id"] for o in ctx["observations"] if o["tool"] == "request_revision"}
        for brand_id, v in verdicts.items():
            if v["verdict"] == "revise" and v["revisions_left"] > 0 and brand_id not in requested:
                return call("request_revision", brand_id=brand_id, constraint="Gold is too pricey for a cafe",
                            max_price_inr=20000, reasoning="price too high for brand size")
        ok = [b for b, v in verdicts.items() if v["verdict"] == "ok" or v["revisions_left"] == 0]
        if ok and "prepare_emails" not in used:
            return call("prepare_emails", brand_ids=ok, reasoning="these are good to go")
    if ctx["send_gate_approved"] and "send_approved" not in used:
        return call("send_approved", reasoning="organizer approved")
    if ctx["polling_requested"]:
        if "poll_replies" not in used:
            return call("poll_replies", reasoning="check inbox")
        polled = _last_result(ctx, "poll_replies")
        if polled["new_replies"] and "classify_replies" not in used:
            return call("classify_replies", reasoning="understand the replies")
        done = {(o["tool"], o["args"].get("brand_id")) for o in ctx["observations"]}
        classified = (_last_result(ctx, "classify_replies") or {}).get("classified", {})
        for brand_id, c in classified.items():
            action = {"pushback_price": "route_pushback", "pushback_deliverables": "route_pushback",
                      "declined": "request_replacement", "accepted": "mark_accepted"}.get(c["label"])
            if action and (action, brand_id) not in done:
                extra = {"why": c["summary"]} if action == "request_replacement" else {}
                return call(action, brand_id=brand_id, reasoning=c["summary"], **extra)
        for brand_id, text in polled["new_replies"].items():
            if text.startswith("BOUNCED") and ("request_replacement", brand_id) not in done:
                return call("request_replacement", brand_id=brand_id, why="email bounced", reasoning="bounced")
        for brand_id in polled["silent_threads"]:
            if ("draft_nudge", brand_id) not in done:
                return call("draft_nudge", brand_id=brand_id, body="Just following up on our proposal.",
                            reasoning="no reply for days")
    return call("finish", summary="outreach handled")


def critique(ctx: dict) -> dict:
    """Asks for a revision when Gold costs more than Rs 20k and a revision is still allowed."""
    out = []
    for item in ctx["items"]:
        gold = max(t["price_inr"] for t in item["proposal"]["tiers"])
        revise = gold > 20000 and item["revisions_left"] > 0
        out.append({"brand_id": item["brand"]["id"], "verdict": "revise" if revise else "ok",
                    "issues": ["Gold price is high for a small cafe"] if revise else [],
                    "email_subject": f"Sponsor {ctx['event']['name']}",
                    "email_body": f"Hi {item['brand']['name']}, please see the attached proposal."})
    return {"critiques": out}


def classify(ctx: dict) -> dict:
    out = []
    for r in ctx["replies"]:
        text = r["reply"].lower()
        if "expensive" in text or "budget" in text:
            out.append({"brand_id": r["brand_id"], "label": "pushback_price", "summary": "price too high",
                        "constraint": "Keep every tier under Rs 15,000", "max_price_inr": 15000})
        elif "not interested" in text:
            out.append({"brand_id": r["brand_id"], "label": "declined", "summary": "not interested this year"})
        elif "go with" in text:
            tier = next(t for t in ("Gold", "Silver", "Bronze") if t.lower() in text)
            out.append({"brand_id": r["brand_id"], "label": "accepted", "summary": f"accepted {tier}",
                        "accepted_tier": tier})
        else:
            out.append({"brand_id": r["brand_id"], "label": "unrelated", "summary": "unclear"})
    return {"replies": out}


class FakeGmail:
    """Records sends; serves scripted inbound messages per thread. Uses the real demo-recipient logic."""

    SENDER = "paytriq.demo@test"

    def __init__(self, settings, fail_send: bool = False) -> None:
        from app.tools.gmail import GmailTool

        self.real = GmailTool(settings)
        self.fail_send = fail_send
        self.sent: list[dict] = []
        self.threads: dict[str, list[dict]] = {}

    def recipient_for(self, contact_email, index):
        return self.real.recipient_for(contact_email, index)

    def is_own(self, address: str) -> bool:
        return address == self.SENDER

    async def send(self, to, subject, body, attachment=None, thread_id=None, in_reply_to=None) -> dict:
        if self.fail_send:
            raise RuntimeError("Gmail token refresh failed (400): invalid_grant")
        thread_id = thread_id or f"thread-{len(self.sent) + 1}"
        self.sent.append({"to": to, "subject": subject, "body": body, "attachment": attachment,
                          "thread_id": thread_id, "in_reply_to": in_reply_to})
        return {"message_id": f"m-{len(self.sent)}", "thread_id": thread_id}

    def reply(self, thread_id: str, text: str, sender: str = "owner@brand.in", bounce: bool = False) -> None:
        msgs = self.threads.setdefault(thread_id, [])
        msgs.append({"id": f"r-{thread_id}-{len(msgs)}", "from": sender, "subject": "Re", "text": text,
                     "message_id_header": f"<r{len(msgs)}@mail>", "date_ms": 0, "is_bounce": bounce})

    async def get_thread(self, thread_id: str) -> list[dict]:
        return list(self.threads.get(thread_id, []))
