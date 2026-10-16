"""Evidence verification with Gemini Vision.

For one brand, all of its evidence (organizer photos + links) is checked against every promised
deliverable in a single multimodal call. Links are fetched and reduced to plain text first. The model
must answer per deliverable with verified / not_verified / ambiguous / missing and cite evidence ids;
anything it does not mention is treated as ambiguous rather than assumed delivered.
"""

from __future__ import annotations

import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app.core.llm import ImageInput

MAX_IMAGES = 6
Status = Literal["verified", "not_verified", "ambiguous", "missing"]

SYSTEM = """You audit sponsorship deliverables for a college event. For EACH promised deliverable decide,
from the photos and link texts only, whether it was delivered:
- verified: the evidence clearly shows it (e.g. the brand's logo is legible on the stage banner);
- not_verified: the evidence clearly shows it was NOT done (e.g. the banner shows other sponsors only);
- ambiguous: something relevant exists but you cannot be sure (blurry, logo not readable, wrong angle);
- missing: there is no evidence about this deliverable at all.
Never guess. Cite the evidence ids you relied on and explain in one sentence."""


class DeliverableCheck(BaseModel):
    deliverable: str
    status: Status
    confidence: float = Field(ge=0, le=1)
    explanation: str
    evidence_ids: list[str] = []


class BrandVerification(BaseModel):
    checks: list[DeliverableCheck]


def html_to_text(html: str, limit: int = 3000) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:limit]


async def fetch_link_text(url: str, timeout: float = 10.0) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": "Paytriq/0.1 (student project)"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return html_to_text(resp.text)
    except Exception as exc:
        return f"(could not fetch this link: {type(exc).__name__})"


class VisionTool:
    def __init__(self, llm, storage) -> None:
        self.llm = llm
        self.storage = storage

    async def verify(self, brand_name: str, deliverables: list[str], evidence: list[dict]) -> list[DeliverableCheck]:
        if not evidence:
            return [DeliverableCheck(deliverable=d, status="missing", confidence=1.0,
                                     explanation="No evidence was uploaded for this brand.") for d in deliverables]
        images, photos, links = [], [], []
        for item in evidence:
            if item["kind"] == "photo" and len(images) < MAX_IMAGES:
                data = await self.storage.download("evidence", item["storage_path"])
                images.append(ImageInput(data, item.get("mime_type") or "image/jpeg"))
                photos.append({"image_index": len(images) - 1, "evidence_id": item["id"], "note": item.get("note")})
            elif item["kind"] == "link":
                links.append({"evidence_id": item["id"], "url": item["url"], "note": item.get("note"),
                              "page_text": await fetch_link_text(item["url"])})
        result = await self.llm.generate(
            purpose="roi.vision", system=SYSTEM, schema=BrandVerification, images=images,
            context={"brand": brand_name, "deliverables": deliverables, "photos_in_order": photos, "links": links},
        )
        by_name = {c.deliverable.strip().lower(): c for c in result.checks}
        return [by_name.get(d.strip().lower()) or DeliverableCheck(
            deliverable=d, status="ambiguous", confidence=0.0,
            explanation="The vision check did not report on this deliverable.") for d in deliverables]
