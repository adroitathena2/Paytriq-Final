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
