"""
teams_bot.py — Microsoft Teams Outgoing Webhook receiver.

Responsibilities
----------------
1. Validate the HMAC-SHA256 signature on every inbound Teams request.
2. Parse the Teams Outgoing Webhook JSON payload and extract the clean
   message text (stripping @mention prefixes).
3. Provide a helper to build the JSON response body that Teams expects.

Security model
--------------
Teams Outgoing Webhooks sign every request with HMAC-SHA256 using the
secret provided at webhook creation time.  We validate this signature
before processing any message.  Requests with missing or invalid
signatures are rejected with HTTP 401.

The secret is stored in TEAMS_OUTGOING_WEBHOOK_SECRET (env var / K8s Secret).
If the secret is not configured, all incoming webhook requests are rejected.

Teams message format (inbound)
------------------------------
{
  "type": "message",
  "text": "<at>BotName</at> show pod restarts in namespace prod",
  "from": {"name": "Jane Smith", "id": "29:..."},
  "channelData": {"channel": {"id": "...", "name": "..."}},
  ...
}

Teams response format (outbound)
---------------------------------
{"type": "message", "text": "**Summary:** ..."}

Teams renders the "text" field as Markdown in the channel thread.
"""

import base64
import hashlib
import hmac
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HMAC signature validation
# ---------------------------------------------------------------------------


def validate_teams_signature(
    authorization_header: str | None,
    request_body: bytes,
    webhook_secret: str,
) -> bool:
    """
    Validate the Teams Outgoing Webhook HMAC-SHA256 signature.

    Teams sets the Authorization header as:
        HMAC <base64-encoded-signature>

    The signature is HMAC-SHA256 over the raw request body bytes using the
    webhook secret (which Teams provides as a base64-encoded key).

    Parameters
    ----------
    authorization_header : str | None
        Value of the HTTP Authorization header.
    request_body : bytes
        Raw request body bytes (before JSON parsing).
    webhook_secret : str
        The webhook secret from Teams (base64-encoded), stored in config.

    Returns
    -------
    bool — True if signature is valid, False otherwise.
    """
    if not authorization_header:
        logger.warning("Teams webhook request missing Authorization header.")
        return False

    if not authorization_header.startswith("HMAC "):
        logger.warning(
            "Teams webhook Authorization header has unexpected format: %s",
            authorization_header[:20],
        )
        return False

    received_sig_b64 = authorization_header[5:].strip()

    try:
        # Teams secret is base64-encoded — decode it to raw bytes first
        secret_bytes = base64.b64decode(webhook_secret)
    except Exception:
        logger.error("TEAMS_OUTGOING_WEBHOOK_SECRET is not valid base64.")
        return False

    expected_sig = hmac.new(secret_bytes, request_body, hashlib.sha256).digest()
    expected_sig_b64 = base64.b64encode(expected_sig).decode()

    if not hmac.compare_digest(expected_sig_b64, received_sig_b64):
        logger.warning("Teams webhook HMAC signature mismatch — request rejected.")
        return False

    return True


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

# Teams wraps @mentions in <at>Name</at> tags
_AT_MENTION_RE = re.compile(r"<at>[^<]*</at>", re.IGNORECASE)
# Collapse extra whitespace
_WHITESPACE_RE = re.compile(r"\s+")


def extract_message_text(teams_payload: dict) -> str:
    """
    Extract the clean message text from a Teams Outgoing Webhook payload.

    Removes <at>BotName</at> mention tags and trims whitespace so the
    Conversation Agent receives a clean natural-language question.

    Parameters
    ----------
    teams_payload : dict
        Parsed JSON body from the Teams Outgoing Webhook POST.

    Returns
    -------
    str — Clean message text, or empty string if none found.
    """
    raw_text = teams_payload.get("text", "")
    if not raw_text:
        return ""

    # Strip @mention tags
    clean = _AT_MENTION_RE.sub("", raw_text)
    # Collapse whitespace and trim
    clean = _WHITESPACE_RE.sub(" ", clean).strip()
    return clean


def extract_sender_name(teams_payload: dict) -> str:
    """
    Extract the display name of the Teams message sender.
    Returns empty string if not available.
    Used only for reply formatting — never passed to Prometheus or Claude as input.
    """
    return teams_payload.get("from", {}).get("name", "") or ""


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------


def build_teams_response(text: str) -> dict:
    """
    Build the JSON response body that Teams expects from an Outgoing Webhook.

    Teams renders the 'text' field as Markdown in the channel reply thread.
    Max safe length is ~28,000 characters.

    Parameters
    ----------
    text : str
        The reply text (Markdown supported).

    Returns
    -------
    dict — JSON-serialisable response payload.
    """
    MAX_LEN = 27_000
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "\n\n*(message truncated)*"
    return {"type": "message", "text": text}
