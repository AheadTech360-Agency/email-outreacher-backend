"""Build a timeline from recorded events; never infer milestone timestamps."""
from datetime import UTC


def build_timeline(person, company=None, posts=(), emails=(), feedbacks=(), events=()):
    items = []
    def add(key, at, kind, title, detail="", email_id=None):
        if at is None: return
        if at.tzinfo is None: at = at.replace(tzinfo=UTC)
        items.append({"id": key, "at": at.astimezone(UTC).isoformat(), "kind": kind, "title": title, "detail": detail or "", "emailId": str(email_id) if email_id else None})
    add(f"person:{person.id}", person.found_at, "person", "Person added", person.source)
    add(f"revealed:{person.id}", person.revealed_at, "person", "Contact details revealed")
    if company:
        add(f"research:{company.id}", company.researched_at, "research", "Company researched", company.name)
    for post in posts:
        add(f"post:{post.id}", post.fetched_at, "post", "Post fetched", f"{post.source}: {post.text[:160]}")
    for email in emails:
        add(f"draft:{email.id}", email.created_at, "email", f"Email {email.step} drafted", email.subject, email.id)
        add(f"approved:{email.id}", email.approved_at, "email", "Email approved", email.subject, email.id)
        add(f"queued:{email.id}", email.queued_at, "email", "Email queued", email.subject, email.id)
        add(f"sent:{email.id}", email.sent_at, "sent", f"Email {email.step} sent", email.subject, email.id)
    for feedback in feedbacks:
        add(f"feedback:{feedback.id}", feedback.updated_at, "feedback", "Email rated " + feedback.rating, feedback.note, feedback.email_id)
    for event in events:
        # Exact provider events are retained, including opens, replies, bounces and unsubscribes.
        add(f"event:{event.id}", event.occurred_at, "event", "Sending event: " + event.type, event.provider, event.email_id)
    return sorted(items, key=lambda item: (item["at"], item["id"]), reverse=True)[:100]
