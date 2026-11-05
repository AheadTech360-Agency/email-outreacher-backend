"""Deterministic product field validation; no model calls."""
import re

BUILTIN_KEYS = ("name", "description", "value", "proof", "offer", "cta", "url")
RESERVED = set(BUILTIN_KEYS) | {"notes", "details", "custom", "custom_fields", "what_it_is", "what_it_does_for_them", "pricing", "pricing_or_offer", "call_to_action", "website", "product_name"}


def field_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def validate_fields(fields, notes):
    keys, labels, bodies, ids = set(), set(), set(), set()
    for field in fields:
        key, label = field.key, field_key(field.label)
        if key in RESERVED or label in RESERVED:
            raise ValueError(f"Field '{field.label}' duplicates a built-in product field")
        if key in keys or label in labels:
            raise ValueError(f"Redundant field '{field.label}': field names and variable keys must be unique")
        keys.add(key); labels.add(label)
    if any(f.key == field_key(other.label) for i, f in enumerate(fields) for j, other in enumerate(fields) if i != j):
        raise ValueError("A field name duplicates another field's variable key")
    for note in notes:
        body = " ".join(note.body.casefold().split())
        if note.id in ids or body in bodies:
            raise ValueError("Redundant note: each note must have a unique ID and text")
        ids.add(note.id); bodies.add(body)
    if sum(len(f.value) for f in fields) + sum(len(n.body) for n in notes) > 24000:
        raise ValueError("Custom fields and notes must total at most 24,000 characters")
