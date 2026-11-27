"""Tool registration and HTTP adapter for the Meishi connector."""

import base64
import json
from urllib.parse import urlencode
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
            "email": {"type": ["string", "null"], "format": "email"},
            "email_source_url": {"type": ["string", "null"]},
            "email_checked_at": {"type": ["string", "null"]},
            "email_evidence": {"type": ["string", "null"]},
            "do_not_contact": {"type": "boolean"},
            "sequence_step": {"type": "integer", "minimum": 0},
            "next_followup_at": {"type": ["string", "null"], "format": "date"},
            "segment": {"type": ["string", "null"]},
            "batch_id": {"type": ["string", "null"]},
            "tags": {"type": ["string", "null"]},
        },
        "required": ["status", "date", "channel"],
        "anyOf": [{"required": [key]} for key in ("company", "record_id", "source_url")],
        "additionalProperties": False,
    },
}


LEAD_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(LEAD_STATUSES)},
        "segment": {"type": "string"},
        "batch_id": {"type": "string"},
        "due_on_or_before": {"type": "string", "format": "date"},
        "include_suppressed": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 0},
        "offset": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}
LIST_LEADS_TOOL = {
    "name": "list_leads",
    "description": "List Meishi leads with optional CRM and follow-up filters.",
    "inputSchema": LEAD_FILTER_SCHEMA,
}
EXPORT_LEADS_CSV_TOOL = {
    "name": "export_leads_csv",
    "description": "Export filtered Meishi leads as CSV text.",
    "inputSchema": LEAD_FILTER_SCHEMA,
}
ADD_SUPPRESSION_TOOL = {
    "name": "add_suppression",
    "description": "Suppress an email address or domain in Meishi.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["email", "domain"]},
            "value": {"type": "string"},
            "reason": {"type": "string"},
            "note": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["kind", "value", "reason"],
        "additionalProperties": False,
    },
}
CHECK_SUPPRESSION_TOOL = {
    "name": "check_suppression",
    "description": "Check whether an email address or its domain is suppressed in Meishi.",
    "inputSchema": {
        "type": "object",
        "properties": {"address": {"type": "string", "format": "email"}},
        "required": ["address"],
        "additionalProperties": False,
    },
}


def _request(path, args, base_url, password, transport, *, method="GET", csv=False):
    url = base_url.rstrip("/") + path
    data = None
    headers = {}
    if method == "POST":
        data = json.dumps(args, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Meishi": "1"}
    elif args:
        url += "?" + urlencode({key: str(value).lower() if isinstance(value, bool) else value
                                for key, value in args.items()})
    request = Request(url, data=data, headers=headers, method=method)
    if password is not None:
        credentials = base64.b64encode(f"meishi:{password}".encode()).decode("ascii")
        request.add_unredirected_header("Authorization", f"Basic {credentials}")
    with (transport or urlopen)(request, timeout=10) as response:
        return response.read().decode("utf-8-sig") if csv else json.load(response)


def update_lead_status(args, base_url, password=None, *, transport=None):
    return _request("/api/leads/status", args, base_url, password, transport, method="POST")


def list_leads(args, base_url, password=None, *, transport=None):
    return _request("/api/leads", args, base_url, password, transport)


def export_leads_csv(args, base_url, password=None, *, transport=None):
    return _request("/api/leads/export.csv", args, base_url, password, transport, csv=True)


def add_suppression(args, base_url, password=None, *, transport=None):
    return _request("/api/suppressions", args, base_url, password, transport, method="POST")


def check_suppression(args, base_url, password=None, *, transport=None):
    return _request("/api/suppressions/check", args, base_url, password, transport)
