"""Renders a sponsorship proposal as a clean one-to-two page PDF (ReportLab) and stores it."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.graph.state import Brand, Proposal

ACCENT = colors.HexColor("#4F46E5")  # indigo-600, same accent as the web app
GREY = colors.HexColor("#475569")
LINE = colors.HexColor("#E2E8F0")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontSize=20, textColor=colors.HexColor("#0F172A"),
                                alignment=0, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontSize=11, textColor=GREY, spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, textColor=ACCENT, spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=10.5, leading=15),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=9.5, leading=13),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=8.5, textColor=GREY),
    }


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_proposal_pdf(event: dict, brand: Brand, proposal: Proposal) -> bytes:
    s = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{event['name']} x {brand.name}", author="Paytriq")
    story = [
        Paragraph(f"{_esc(event['name'])} &times; {_esc(brand.name)}", s["title"]),
        Paragraph(f"Sponsorship proposal &middot; {_esc(event['college'])} &middot; {_esc(event['date'])}", s["subtitle"]),
        Paragraph("Why this partnership", s["h2"]),
        Paragraph(_esc(proposal.pitch).replace("\n", "<br/>"), s["body"]),
        Paragraph("The event", s["h2"]),
    ]
    facts = [
        ["Expected footfall", f"{event['expected_footfall']:,} attendees"],
        ["Audience", _esc(event["audience"])],
        ["Your distance from campus", f"{brand.distance_km} km"],
    ]
    facts_table = Table([[Paragraph(k, s["cell"]), Paragraph(v, s["cell"])] for k, v in facts], colWidths=[50 * mm, None])
    facts_table.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                     ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [facts_table, Paragraph("Sponsorship tiers", s["h2"])]

    rows = [[Paragraph(f"<b>{h}</b>", s["cell"]) for h in ("Tier", "Investment", "What you get", "Expected reach")]]
    for tier in proposal.tiers:
        deliverables = "<br/>".join(f"&bull; {_esc(d)}" for d in tier.deliverables)
        rows.append([Paragraph(f"<b>{tier.name}</b>", s["cell"]), Paragraph(f"Rs. {tier.price_inr:,}", s["cell"]),
                     Paragraph(deliverables, s["cell"]), Paragraph(f"~{tier.expected_reach:,} people", s["cell"])])
    tiers_table = Table(rows, colWidths=[22 * mm, 28 * mm, None, 30 * mm], repeatRows=1)
    tiers_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2FF")), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [
        tiers_table,
        Spacer(1, 8 * mm),
        Paragraph("Expected reach combines on-ground visibility at the event with the organizers' social posts. "
                  "After the event you receive a verified ROI report with photo evidence for every deliverable.",
                  s["small"]),
        Spacer(1, 3 * mm),
        Paragraph(f"Proposal v{proposal.version} &middot; generated {datetime.now(UTC):%d %b %Y} with Paytriq", s["small"]),
    ]
    doc.build(story)
    return buffer.getvalue()


class PdfTool:
    """Renders the PDF and uploads it to the public ``proposals`` bucket. Returns the public URL."""

    def __init__(self, storage) -> None:
        self.storage = storage

    async def render_and_store(self, run_id: str, event: dict, brand: Brand, proposal: Proposal) -> str:
        data = render_proposal_pdf(event, brand, proposal)
        path = f"{run_id}/{brand.id}-v{proposal.version}.pdf"
        return await self.storage.upload("proposals", path, data, "application/pdf")
