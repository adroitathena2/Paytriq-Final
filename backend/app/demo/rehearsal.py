"""Offline rehearsal: a scripted stand-in for Gemini, for practising the demo without network or quota.

NOT real reasoning. Every trace entry of a rehearsal run is marked ``REHEARSAL_MARK`` (core/trace.py) and
its export is named ``REHEARSAL-NOT-FOR-SUBMISSION-...``. A rehearsal trace must never go in ``traces/``.

Each policy reads the same context the real model would get and returns a plausible, fixed answer, so the
agents' code paths (tools, guardrails, arbitration, loops) run exactly as they do with Gemini.
"""

from __future__ import annotations

import re

from app.agents.proposal import CATEGORY_BUDGET, NO_FIT_CATEGORIES
from app.core.clef import FakeClef
from app.core.llm import FakeLLM
from app.core.trace import REHEARSAL_MARK  # noqa: F401  (re-exported: the marker every rehearsal entry carries)
from app.demo.world import rehearsal_fit


def _call(name: str, reasoning: str = "rehearsal script", **args) -> dict:
    return {"name": name, "args": args, "reasoning": reasoning}


def _used(ctx: dict) -> list[str]:
    return [o["tool"] for o in ctx.get("observations", [])]


def _last(ctx: dict, tool: str) -> dict | None:
    return next((o["result"] for o in reversed(ctx.get("observations", [])) if o["tool"] == tool), None)


def supervisor_route(ctx: dict) -> dict:
    for agent, requests in ctx["open_requests"].items():
        if requests:
            return {"next": agent, "reason": f"{agent} has {len(requests)} open request(s)."}
    table = {"discovery": ("discovery", None), "shortlist_review": ("human_approval", "shortlist"),
             "proposals": ("proposal", None), "review": ("outreach", None), "send_review": ("human_approval", "send"),
             "sending": ("outreach", None), "replies": ("outreach", None), "replacement": ("discovery", None),
             "roi": ("roi_audit", None), "roi_review": ("human_approval", "roi_publish")}
    nxt, gate = table.get(ctx["phase"], ("END", None))
    return {"next": nxt, "gate": gate, "reason": f"Phase {ctx['phase']} leads to {nxt}."}


def discovery_plan(ctx: dict) -> dict:
    used, replan = _used(ctx), ctx.get("replan")
    if not used:
        if replan:
            return _call("search_places", categories=replan["categories"], radius_km=replan["radius_km"])
        return _call("search_places", categories=["cafe", "gym"], radius_km=2)
    if "score_brands" not in used:
        return _call("score_brands")
    return _call("finish", summary="ranked the candidates")


def discovery_score(ctx: dict) -> dict:
    return {"scores": [{"id": b["id"], "fit_score": rehearsal_fit(b["id"]), "fit_reason": "scripted rehearsal score"}
                       for b in ctx["brands"]]}


def discovery_replace(ctx: dict) -> dict:
    used = _used(ctx)
    if "list_alternatives" not in used:
        return _call("list_alternatives")
    options = (_last(ctx, "list_alternatives") or {}).get("options", [])
    if "pick_replacement" not in used and options:
        return _call("pick_replacement", brand_id=options[0]["id"], why="next best unused brand")
    return _call("finish", summary="replacement chosen")


def proposal_plan(ctx: dict) -> dict:
    used, obs = _used(ctx), ctx.get("observations", [])
    if not used:
        return _call("draft_proposal", guidance="")
    if obs[-1]["tool"] == "draft_proposal":
        return _call("check_pricing") if obs[-1]["result"]["ok"] else _call("finish", summary="could not draft")
    if "render_pdf" not in used:
        return _call("render_pdf")
    return _call("finish", summary="proposal ready")


def proposal_write(ctx: dict) -> dict:
    gold = ctx.get("max_price_inr") or int(ctx["typical_budget_inr"] * 1.2)  # a little ambitious on purpose
    return {"pitch": f"{ctx['brand']['name']} meets {ctx['event']['expected_footfall']:,} students next door.",
            "tiers": [{"name": "Bronze", "price_inr": int(gold * 0.3), "deliverables": ["Logo on event posters"]},
                      {"name": "Silver", "price_inr": int(gold * 0.6),
                       "deliverables": ["Logo on event posters", "Stall at the venue"]},
                      {"name": "Gold", "price_inr": gold, "deliverables": [
                          "Logo on the main stage banner", "Stall at the venue", "1 Instagram post on the fest handle"]}],
            "changes_made": f"v{ctx['version']}: " + (ctx.get("revision_constraint") or "first draft")}


def outreach_critique(ctx: dict) -> dict:
    out = []
    for item in ctx["items"]:
        gold = max(t["price_inr"] for t in item["proposal"]["tiers"])
        budget = CATEGORY_BUDGET.get(item["brand"]["category"], 30000)
        revise = gold > budget and item["revisions_left"] > 0
        out.append({"brand_id": item["brand"]["id"], "verdict": "revise" if revise else "ok",
                    "issues": [f"Gold at Rs {gold:,} is above what a {item['brand']['category']} usually spends"]
                    if revise else [],
                    "email_subject": f"{ctx['event']['name']} x {item['brand']['name']}",
                    "email_body": f"Hi {item['brand']['name']} team, please find our sponsorship proposal attached."})
    return {"critiques": out}


def outreach_plan(ctx: dict) -> dict:
    used = _used(ctx)
    if ctx["drafts_to_review"]:
        if "critique_proposals" not in used:
            return _call("critique_proposals", brand_ids=ctx["drafts_to_review"])
        verdicts = _last(ctx, "critique_proposals")["verdicts"]
        asked = {o["args"]["brand_id"] for o in ctx["observations"] if o["tool"] == "request_revision"}
        for brand_id, v in verdicts.items():
            if v["verdict"] == "revise" and v["revisions_left"] > 0 and brand_id not in asked:
                return _call("request_revision", brand_id=brand_id, max_price_inr=20000,
                             constraint="Gold is too pricey for this business; keep it at or under Rs 20,000")
        ok = [b for b, v in verdicts.items() if v["verdict"] == "ok" or v["revisions_left"] == 0]
        if ok and "prepare_emails" not in used:
            return _call("prepare_emails", brand_ids=ok)
    if ctx["send_gate_approved"] and "send_approved" not in used:
        return _call("send_approved")
    if ctx["polling_requested"]:
        if "poll_replies" not in used:
            return _call("poll_replies")
        polled = _last(ctx, "poll_replies")
        if polled["new_replies"] and "classify_replies" not in used:
            return _call("classify_replies")
        done = {(o["tool"], o["args"].get("brand_id")) for o in ctx["observations"]}
        for brand_id, c in ((_last(ctx, "classify_replies") or {}).get("classified") or {}).items():
            action = {"pushback_price": "route_pushback", "declined": "request_replacement",
                      "accepted": "mark_accepted"}.get(c["label"])
            if action and (action, brand_id) not in done:
                extra = {"why": c["summary"]} if action == "request_replacement" else {}
                return _call(action, brand_id=brand_id, **extra)
        for brand_id, text in polled["new_replies"].items():
            if text.startswith("BOUNCED") and ("request_replacement", brand_id) not in done:
                return _call("request_replacement", brand_id=brand_id, why="email bounced")
        for brand_id in polled["silent_threads"]:
            if ("draft_nudge", brand_id) not in done:
                return _call("draft_nudge", brand_id=brand_id, body="Just following up on our proposal.")
    return _call("finish", summary="outreach handled")


def outreach_classify(ctx: dict) -> dict:
    out = []
    for r in ctx["replies"]:
        text = r["reply"].lower()
        if "go with" in text:
            tier = next(t for t in ("Gold", "Silver", "Bronze") if t.lower() in text)
            out.append({"brand_id": r["brand_id"], "label": "accepted", "summary": f"accepted {tier}",
                        "accepted_tier": tier})
        elif "not interested" in text:
            out.append({"brand_id": r["brand_id"], "label": "declined", "summary": "not interested"})
        elif "max" in text or "budget" in text:
            amount = re.search(r"rs\.?\s*([\d,]+)", text)
            cap = int(amount.group(1).replace(",", "")) if amount else None
            out.append({"brand_id": r["brand_id"], "label": "pushback_price", "summary": "price too high",
                        "constraint": f"Keep every tier at or under Rs {cap:,}" if cap else "Lower the price",
                        "max_price_inr": cap})
        else:
            out.append({"brand_id": r["brand_id"], "label": "unrelated", "summary": "unclear"})
    return {"replies": out}


def roi_plan(ctx: dict) -> dict:
    used = _used(ctx)
    if "list_deliverables" not in used:
        return _call("list_deliverables")
    verified = {o["args"]["brand_id"] for o in ctx["observations"] if o["tool"] == "verify_brand"}
    for brand_id in ctx["sponsors"]:
        if brand_id not in verified:
            return _call("verify_brand", brand_id=brand_id)
    if "compile_report" not in used:
        return _call("compile_report", summary="Scripted rehearsal audit.")
    return _call("finish", summary="report compiled")


def roi_vision(ctx: dict) -> dict:
    checks = []
    for d in ctx["deliverables"]:
        photo = next((p for p in ctx["photos_in_order"] if (p["note"] or "").lower() == d.lower()), None)
        checks.append({"deliverable": d, "status": "verified" if photo else "missing", "confidence": 0.9,
                       "explanation": "scripted rehearsal check",
                       "evidence_ids": [photo["evidence_id"]] if photo else []})
    return {"checks": checks}


def rehearsal_llm() -> FakeLLM:
    return FakeLLM(
        decide={"discovery.plan": discovery_plan, "discovery.replace": discovery_replace,
                "proposal.plan": proposal_plan, "outreach.plan": outreach_plan, "roi.plan": roi_plan},
        generate={"supervisor.route": supervisor_route, "discovery.score": discovery_score,
                  "discovery.replan": {"radius_km": 4, "extra_categories": ["coaching", "bakery", "salon"],
                                       "reasoning": "Rehearsal: too few good fits within 2 km; widen to 4 km."},
                  "proposal.write": proposal_write, "outreach.critique": outreach_critique,
                  "outreach.classify": outreach_classify, "roi.vision": roi_vision,
                  "arbitration.justify": lambda ctx: {"text": f"Rehearsal (scripted): {ctx['resolution']}"}},
    )


def rehearsal_clef() -> FakeClef:
    """Scripted Clef that gives the old rules' answers with confident probabilities (never the real model)."""
    return FakeClef({
        "proposal.feasibility": lambda s: 0.05 if s["brand"]["category"] in NO_FIT_CATEGORIES else 0.95,
        "outreach.category_feedback": 0.9,
    })
