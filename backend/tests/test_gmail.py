"""Gmail tool helpers (no network)."""

import base64
import email

import pytest

from app.tools.base import ToolError
from app.tools.gmail import GmailTool, build_mime, parse_message, strip_quoted


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_parse_reply_drops_quoted_text():
    msg = {"id": "x1", "internalDate": "1700000000000", "payload": {
        "headers": [{"name": "From", "value": "Cafe Owner <owner@cafe.in>"}, {"name": "Subject", "value": "Re: Sponsor"},
                    {"name": "Message-ID", "value": "<abc@mail>"}],
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/plain", "body": {"data": b64("Too pricey.\n\nOn Mon, Paytriq wrote:\n> Hi")}}]}}
    parsed = parse_message(msg)
    assert parsed["from"] == "owner@cafe.in" and parsed["text"] == "Too pricey."
    assert parsed["message_id_header"] == "<abc@mail>" and not parsed["is_bounce"]


def test_bounce_is_detected():
    msg = {"id": "b", "payload": {"headers": [{"name": "From", "value": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"}]}}
    assert parse_message(msg)["is_bounce"]


def test_strip_quoted_keeps_plain_reply():
    assert strip_quoted("Yes, Silver works.\n> quoted line") == "Yes, Silver works."


def test_mime_has_threading_headers_and_pdf():
    raw = build_mime("me@x.in", "you@y.in", "Hi", "Body", ("p.pdf", b"%PDF-1.4"), "<orig@mail>")
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["In-Reply-To"] == "<orig@mail>"
    assert [p.get_filename() for p in parsed.walk() if p.get_filename()] == ["p.pdf"]


def test_demo_mode_redirects_round_robin(settings):
    tool = GmailTool(settings)
    assert tool.recipient_for("real@shop.in", 0) == ("team1@example.com", True)
    assert tool.recipient_for(None, 3) == ("team2@example.com", True)


def test_live_mode_needs_a_contact(settings):
    tool = GmailTool(settings.model_copy(update={"demo_mode": False}))
    assert tool.recipient_for("real@shop.in", 0) == ("real@shop.in", False)
    with pytest.raises(ToolError):
        tool.recipient_for(None, 0)


async def test_unconfigured_gmail_fails_cleanly(settings):
    with pytest.raises(ToolError, match="not configured"):
        await GmailTool(settings).send("a@b.c", "s", "b")
