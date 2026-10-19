"""Gmail API over plain HTTPS with an OAuth refresh token (dedicated demo sender account).

DEMO_MODE (default on in deployment): every email goes to DEMO_RECIPIENTS (team inboxes) instead of the
real business, and the real brand name is put in the subject, so no real business is ever emailed.
Replies are real: the team replies from those inboxes and ``get_thread`` reads them back.
"""

from __future__ import annotations

import base64
import re
import time
from email.message import EmailMessage
from email.utils import parseaddr

import httpx

from app.tools.base import ToolError

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
QUOTE_START = re.compile(r"^(On .+ wrote:|-----Original Message-----|From: .+)$", re.MULTILINE)


def demo_subject(brand_name: str, subject: str) -> str:
    return f"[Paytriq demo -> {brand_name}] {subject}"


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply (drop the quoted previous email)."""
    match = QUOTE_START.search(text)
    text = text[: match.start()] if match else text
    return "\n".join(line for line in text.splitlines() if not line.startswith(">")).strip()


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _plain_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


def parse_message(msg: dict) -> dict:
    headers = msg.get("payload", {}).get("headers", [])
    sender = parseaddr(_header(headers, "From"))[1].lower()
    return {
        "id": msg["id"],
        "from": sender,
        "subject": _header(headers, "Subject"),
        "message_id_header": _header(headers, "Message-ID"),
        "date_ms": int(msg.get("internalDate", "0")),
        "text": strip_quoted(_plain_text(msg.get("payload", {})) or msg.get("snippet", "")),
        "is_bounce": sender.startswith(BOUNCE_SENDERS) or "delivery status notification" in _header(headers, "Subject").lower(),
    }


def build_mime(sender: str, to: str, subject: str, body: str, attachment: tuple[str, bytes] | None,
               in_reply_to: str | None) -> str:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    if in_reply_to:
        msg["In-Reply-To"] = msg["References"] = in_reply_to
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailTool:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._token: str | None = None
        self._token_expiry = 0.0

    @property
    def configured(self) -> bool:
        s = self.settings
        return bool(s.gmail_client_id and s.gmail_client_secret and s.gmail_refresh_token and s.gmail_sender)

    def recipient_for(self, contact_email: str | None, index: int) -> tuple[str, bool]:
        """Returns (address, redirected_to_demo_inbox)."""
        if self.settings.demo_mode:
            demo = self.settings.demo_recipient_list
            if not demo:
                raise ToolError("DEMO_MODE is on but DEMO_RECIPIENTS is empty")
            return demo[index % len(demo)], True
        if not contact_email:
            raise ToolError("brand has no contact email")
        return contact_email, False

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if not self.configured:
            raise ToolError("Gmail is not configured (GMAIL_* variables missing)")
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = await client.post(TOKEN_URL, data={
            "client_id": self.settings.gmail_client_id, "client_secret": self.settings.gmail_client_secret,
            "refresh_token": self.settings.gmail_refresh_token, "grant_type": "refresh_token",
        })
        if resp.status_code != 200:
            raise ToolError(f"Gmail token refresh failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        self._token, self._token_expiry = data["access_token"], time.time() + data.get("expires_in", 3600)
        return self._token

    async def send(self, to: str, subject: str, body: str, attachment: tuple[str, bytes] | None = None,
                   thread_id: str | None = None, in_reply_to: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._access_token(client)
            payload = {"raw": build_mime(self.settings.gmail_sender, to, subject, body, attachment, in_reply_to)}
            if thread_id:
                payload["threadId"] = thread_id
            resp = await client.post(f"{API}/messages/send", json=payload, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code >= 300:
                raise ToolError(f"Gmail send failed ({resp.status_code}): {resp.text[:200]}")
            data = resp.json()
            return {"message_id": data["id"], "thread_id": data["threadId"]}

    async def get_thread(self, thread_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._access_token(client)
            resp = await client.get(f"{API}/threads/{thread_id}", params={"format": "full"},
                                    headers={"Authorization": f"Bearer {token}"})
            if resp.status_code >= 300:
                raise ToolError(f"Gmail thread fetch failed ({resp.status_code})")
            return [parse_message(m) for m in resp.json().get("messages", [])]

    def is_own(self, address: str) -> bool:
        return address.lower() == self.settings.gmail_sender.lower()
