"""Read-only checks. Unknown data must never be presented as ready."""
from datetime import UTC, datetime
from typing import Any


def assess(campaign: dict[str, Any], enrolled: int) -> dict[str, Any]:
    checks = []
    def add(key, status, message):
        checks.append({"key": key, "status": status, "message": message})
    add("leads", "pass" if enrolled else "fail", f"{enrolled} leads enrolled" if enrolled else "No leads enrolled. Queue approved emails first.")
    steps = campaign.get("steps") or []
    missing = []
    for index, step in enumerate(steps, 1):
        variants = [v for v in step.get("variants", []) if not v.get("archived")]
        copies = variants or [step]
        if any(not (v.get("body_text") or v.get("body_html") or "").strip() for v in copies):
            missing.append(index)
    if not steps:
        add("steps", "fail", "No email steps configured. Set up the campaign in CheapSequencer.")
    elif missing:
        add("steps", "fail", "Missing email copy in steps " + ", ".join(map(str, missing)) + ".")
    else:
        add("steps", "pass", f"{len(steps)} email steps have copy. Recipient field values are checked by CheapSequencer at launch.")
    days = campaign.get("sending_days")
    if days is None:
        add("window", "unknown", "The read API does not expose sending days. Verify the sending window in CheapSequencer.")
    elif any(d.get("enabled") and isinstance(d.get("start_hour"), (int, float)) and isinstance(d.get("end_hour"), (int, float))
             and d["end_hour"] > d["start_hour"] for d in days.values()):
        add("window", "pass", "At least one sending day has a valid window.")
    else:
        add("window", "fail", "No enabled sending window. Enable sending days in CheapSequencer.")
    selection = campaign.get("sender_selection") or {}
    ids = campaign.get("inbox_ids", selection.get("inbox_ids"))
    mode = campaign.get("inbox_selection_mode", selection.get("mode"))
    if ids:
        add("senders", "unknown", f"{len(ids)} sending mailboxes selected; mailbox eligibility is checked at launch.")
    elif ids == [] and mode == "manual":
        add("senders", "fail", "No sending mailboxes selected. Choose mailboxes in CheapSequencer.")
    else:
        add("senders", "unknown", "Mailbox eligibility is not exposed here; CheapSequencer validates it at launch.")
    status = campaign.get("status") or "unknown"
    if status == "paused":
        add("state", "fail", "Campaign is paused. Resume it in CheapSequencer when you're ready to send.")
    elif status not in ("draft", "active"):
        add("state", "fail", f"Campaign is {status}, so it can't send.")
    return {"id": campaign["id"], "name": campaign.get("name") or "Campaign", "status": status,
            "timezone": campaign.get("timezone"), "enrolled": enrolled, "checks": checks,
            "checked_at": datetime.now(UTC), "blocking": sum(c["status"] == "fail" for c in checks)}
