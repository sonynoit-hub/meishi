"""Tool registration and HTTP adapter for the Meishi connector."""

import base64
import json
from urllib.request import Request, urlopen

from lead_store import LEAD_STATUSES


UPDATE_LEAD_STATUS_TOOL = {
    "name": "update_lead_status",
    "description": "Set a Meishi lead's status and record a contact.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "company": {"type": "string"},
            "record_id": {"type": "string"},
            "source_url": {"type": "string"},
            "status": {"type": "string", "enum": list(LEAD_STATUSES)},
            "date": {"type": "string", "format": "date"},
            "channel": {"type": "string", "minLength": 1},
            "to_address": {"type": "string"},
            "from_address": {"type": "string"},
            "subject": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["status", "date", "channel"],
        "anyOf": [{"required": [key]} for key in ("company", "record_id", "source_url")],
        "additionalProperties": False,
    },
}


def update_lead_status(args, base_url, password=None, *, transport=None):
    """Forward tool arguments to Meishi and return the decoded response."""
    request = Request(
        base_url.rstrip("/") + "/api/leads/status",
        data=json.dumps(args, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Meishi": "1"},
        method="POST",
    )
    if password is not None:
        credentials = base64.b64encode(f"meishi:{password}".encode()).decode("ascii")
        request.add_unredirected_header("Authorization", f"Basic {credentials}")
    with (transport or urlopen)(request, timeout=10) as response:
        return json.load(response)
