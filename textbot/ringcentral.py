"""RingCentral: read SMS threads, send SMS, manage the webhook subscription.

Auth uses a JWT credential from a RingCentral developer app
(scopes needed: ReadMessages, SMS, SubscriptionWebhook).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx2 as httpx

from textbot.models import InboundText, ThreadMessage
from textbot.phone import normalize_phone, to_e164

EVENT_FILTER = "/restapi/v1.0/account/~/extension/~/message-store/instant?type=SMS"
# RingCentral caps webhook lifetime; we re-check and renew on a timer anyway.
SUBSCRIPTION_SECONDS = 60 * 60 * 24 * 7


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_notification(payload: dict) -> InboundText | None:
    """Turn a webhook notification into an InboundText, or None if it isn't an inbound SMS."""
    body = payload.get("body") or {}
    if body.get("direction") != "Inbound" or body.get("type") != "SMS":
        return None
    conversation = body.get("conversationId") or (body.get("conversation") or {}).get("id")
    from_phone = normalize_phone((body.get("from") or {}).get("phoneNumber"))
    to_list = body.get("to") or [{}]
    our_phone = normalize_phone(to_list[0].get("phoneNumber"))
    if not (body.get("id") and conversation and from_phone):
        return None
    created_raw = body.get("creationTime") or body.get("lastModifiedTime")
    created = parse_time(created_raw) if created_raw else datetime.now(timezone.utc)
    return InboundText(
        message_id=str(body["id"]),
        conversation_id=str(conversation),
        tenant_phone=from_phone,
        our_phone=our_phone,
        text=body.get("subject") or "",
        created=created,
    )


def outbound_conversation_id(payload: dict) -> str | None:
    """Conversation id of an outbound SMS notification (a reply sent from RingCentral), if any."""
    body = payload.get("body") or {}
    if body.get("direction") != "Outbound" or body.get("type") != "SMS":
        return None
    conversation = body.get("conversationId") or (body.get("conversation") or {}).get("id")
    return str(conversation) if conversation else None


class RingCentralClient:
    def __init__(self, server: str, client_id: str, client_secret: str, jwt: str) -> None:
        self._server = server.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._jwt = jwt
        self._token = ""
        self._token_expires = 0.0
        self._http = httpx.AsyncClient(base_url=self._server, timeout=30.0)

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._jwt)

    async def _auth_headers(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_expires - 60:
            resp = await self._http.post(
                "/restapi/oauth/token",
                auth=(self._client_id, self._client_secret),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self._jwt,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    async def get_thread(self, conversation_id: str, lookback_days: int) -> list[ThreadMessage]:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        resp = await self._http.get(
            "/restapi/v1.0/account/~/extension/~/message-store",
            headers=await self._auth_headers(),
            params={
                "conversationId": conversation_id,
                "messageType": "SMS",
                "dateFrom": since.isoformat().replace("+00:00", "Z"),
                "perPage": 100,
            },
        )
        resp.raise_for_status()
        messages = [
            ThreadMessage(
                id=str(r["id"]),
                direction=r.get("direction", ""),
                text=r.get("subject") or "",
                created=parse_time(r["creationTime"]),
            )
            for r in resp.json().get("records", [])
        ]
        return sorted(messages, key=lambda m: m.created)

    async def send_sms(self, from_phone: str, to_phone: str, text: str) -> str:
        resp = await self._http.post(
            "/restapi/v1.0/account/~/extension/~/sms",
            headers=await self._auth_headers(),
            json={
                "from": {"phoneNumber": to_e164(from_phone)},
                "to": [{"phoneNumber": to_e164(to_phone)}],
                "text": text,
            },
        )
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    async def ensure_subscription(self, address: str, verification_token: str) -> str:
        """Make sure an active webhook points at `address`; renew or create it. Returns its id."""
        headers = await self._auth_headers()
        resp = await self._http.get("/restapi/v1.0/subscription", headers=headers)
        resp.raise_for_status()
        for sub in resp.json().get("records", []):
            mode = sub.get("deliveryMode") or {}
            if mode.get("address") == address and sub.get("status") == "Active":
                renew = await self._http.post(
                    f"/restapi/v1.0/subscription/{sub['id']}/renew", headers=headers
                )
                renew.raise_for_status()
                return str(sub["id"])
        create = await self._http.post(
            "/restapi/v1.0/subscription",
            headers=headers,
            json={
                "eventFilters": [EVENT_FILTER],
                "deliveryMode": {
                    "transportType": "WebHook",
                    "address": address,
                    "verificationToken": verification_token,
                },
                "expiresIn": SUBSCRIPTION_SECONDS,
            },
        )
        create.raise_for_status()
        return str(create.json()["id"])

    async def aclose(self) -> None:
        await self._http.aclose()
