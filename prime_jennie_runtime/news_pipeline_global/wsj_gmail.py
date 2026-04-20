"""WSJ Newsletter Gmail collector via Gmail API (OAuth2).

v2 포팅: `prime_jennie/infra/crawlers/wsj_gmail.py`. 경로만 새 위치로 옮겼다.

지원 뉴스레터 (`from:access@interactive.wsj.com` 구독 기반):
  - The 10-Point (daily, ~20:18 KST)
  - Markets A.M.  (daily, ~20:29 KST)
  - Markets P.M.  (daily, ~05:00 KST 추정)
  - What's News   (daily)

초기 설정 (v2 에서 이미 완료):
  1. Google Cloud Console OAuth 2.0 Client ID (Desktop app) → credentials.json
  2. `python -m prime_jennie_runtime.news_pipeline_global.wsj_gmail` → 브라우저 인증 → token.json
  3. v3 컨테이너는 `infra/secrets/gmail/{credentials,token}.json` bind mount
"""

from __future__ import annotations

import base64
import html as html_mod
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_WSJ_SENDER = "access@interactive.wsj.com"
_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_NEWSLETTER_TYPES: list[tuple[str, str]] = [
    ("The 10-Point:", "10-point"),
    ("Markets A.M.:", "markets-am"),
    ("Markets P.M.:", "markets-pm"),
    ("What's News:", "whats-news"),
]

_STRIP_PATTERNS = [
    re.compile(r"Is this email difficult to read\?.*?$", re.MULTILINE),
    re.compile(r"View in browser.*?$", re.MULTILINE),
    re.compile(r"Unsubscribe.*$", re.DOTALL),
    re.compile(r"Copyright \d{4} Dow Jones.*$", re.DOTALL),
    re.compile(r"You are currently subscribed as.*$", re.DOTALL),
    re.compile(r"CONTENT FROM:.*?(?=\n\n[A-Z])", re.DOTALL),
    re.compile(r"MESSAGE FROM:.*?(?=\n\n[A-Z])", re.DOTALL),
    re.compile(r"About Us\n.*$", re.DOTALL),
    re.compile(r"About Me\n.*$", re.DOTALL),
    re.compile(r"Beyond the Newsroom\n.*$", re.DOTALL),
    re.compile(r"Sponsored by.*?(?=\n\n)", re.DOTALL),
]

_URL_PATTERN = re.compile(r"\[https?://[^\]]+\]")
_URL_PATTERN2 = re.compile(r"https?://\S+")

_MULTI_NEWLINE = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_NARROW_SPACE = re.compile(r"[\u2002\u200b\u200c\u200d\u00a0]+")


@dataclass
class WSJNewsletter:
    newsletter_type: str
    subject: str
    body: str
    email_date: str


@dataclass
class WSJBriefing:
    newsletters: list[WSJNewsletter] = field(default_factory=list)

    def to_text(self) -> str:
        if not self.newsletters:
            return ""
        parts = []
        for nl in self.newsletters:
            label = type_to_label(nl.newsletter_type)
            parts.append(f"=== {label} ===\n{nl.subject}\n\n{nl.body}")
        return "\n\n".join(parts)


def type_to_label(ntype: str) -> str:
    labels = {
        "10-point": "WSJ The 10-Point",
        "markets-am": "WSJ Markets A.M.",
        "markets-pm": "WSJ Markets P.M.",
        "whats-news": "WSJ What's News",
    }
    return labels.get(ntype, f"WSJ {ntype}")


def _get_gmail_service(credentials_path: str, token_path: str):
    """Gmail API service (OAuth2). token.json 만료 시 refresh, 그래도 실패 시 재인증 플로우."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_file = Path(token_path)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                logger.warning("Token refresh failed, re-authenticating")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, _SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
        logger.info("Gmail token saved to %s", token_path)

    return build("gmail", "v1", credentials=creds)


def fetch_wsj_briefing(
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
    max_results: int = 10,
) -> WSJBriefing:
    """Gmail API 로 최근 WSJ 뉴스레터 수집 (최대 2일). credentials 없거나 실패 시 빈 결과."""
    briefing = WSJBriefing()

    if not Path(credentials_path).exists():
        logger.debug("Gmail credentials not found at %s, skipping WSJ", credentials_path)
        return briefing

    try:
        service = _get_gmail_service(credentials_path, token_path)

        query = f"from:{_WSJ_SENDER} newer_than:2d"
        result = (
            service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        )
        messages = result.get("messages", [])

        if not messages:
            logger.info("No WSJ emails found in last 2 days")
            return briefing

        logger.info("Found %d WSJ emails", len(messages))

        seen_types: set[str] = set()

        for msg_meta in messages:
            try:
                nl = _fetch_and_parse_gmail(service, msg_meta["id"])
                if nl and nl.newsletter_type not in seen_types:
                    seen_types.add(nl.newsletter_type)
                    briefing.newsletters.append(nl)
            except Exception:
                logger.warning("Failed to parse WSJ email %s", msg_meta["id"], exc_info=True)

            if len(seen_types) >= len(_NEWSLETTER_TYPES):
                break

        logger.info(
            "Collected %d WSJ newsletters: %s",
            len(briefing.newsletters),
            [nl.newsletter_type for nl in briefing.newsletters],
        )

    except FileNotFoundError:
        logger.warning("Gmail token not found — run initial auth first")
    except Exception:
        logger.warning("WSJ Gmail collection failed", exc_info=True)

    return briefing


def _fetch_and_parse_gmail(service, msg_id: str) -> WSJNewsletter | None:
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    subject = headers.get("Subject", "")
    email_date = headers.get("Date", "")

    body_text = _extract_gmail_body(msg["payload"])
    if not body_text:
        return None

    if subject.startswith("Welcome to "):
        return None

    nl_type = _classify_newsletter(subject, body_text)
    if not nl_type:
        return None

    body_text = _clean_body(body_text)

    if len(body_text) < 100:
        return None

    if len(body_text) > 3000:
        body_text = body_text[:3000] + "\n... [truncated]"

    return WSJNewsletter(
        newsletter_type=nl_type,
        subject=subject,
        body=body_text,
        email_date=email_date,
    )


def _extract_gmail_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    parts = payload.get("parts", [])

    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            html = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            return _html_to_text(html)

    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            result = _extract_gmail_body(part)
            if result:
                return result

    return ""


def _normalize_quotes(text: str) -> str:
    return (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _classify_newsletter(subject: str, body: str = "") -> str | None:
    subject_norm = _normalize_quotes(subject)
    for prefix, nl_type in _NEWSLETTER_TYPES:
        if prefix in subject_norm:
            return nl_type

    # Markets P.M./What's News 는 subject prefix 없이 오는 경우가 있어 본문 fallback.
    if body:
        body_lower = _normalize_quotes(body[:1000]).lower()
        if "marketspm" in body_lower or "what happened in markets tod" in body_lower:
            return "markets-pm"
        if "this is an edition of the what's n" in body_lower or "what's news" in body_lower:
            return "whats-news"

    return None


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    return text


def _clean_body(text: str) -> str:
    text = _NARROW_SPACE.sub(" ", text)
    text = _URL_PATTERN.sub("", text)
    text = _URL_PATTERN2.sub("", text)

    for pattern in _STRIP_PATTERNS:
        text = pattern.sub("", text)

    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)

    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    return text.strip()


if __name__ == "__main__":
    """초기 인증 + 수집 스모크.

    Usage: python -m prime_jennie_runtime.news_pipeline_global.wsj_gmail [cred] [token]
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cred_path = sys.argv[1] if len(sys.argv) > 1 else "credentials.json"
    tok_path = sys.argv[2] if len(sys.argv) > 2 else "token.json"

    result = fetch_wsj_briefing(credentials_path=cred_path, token_path=tok_path)

    if result.newsletters:
        print(f"Collected {len(result.newsletters)} newsletters:")
        for nl in result.newsletters:
            print(f"  [{nl.newsletter_type}] {nl.subject} ({nl.email_date})")
    else:
        print("No WSJ newsletters found")
