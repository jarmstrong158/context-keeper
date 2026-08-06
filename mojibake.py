"""Detect and repair cp1252-misdecoded UTF-8 in stored entry text.

Damage written before con-008-dc30 forced UTF-8 on the stdio transport. The
cause is fixed; this is about the entries already on disk, of which there were
160 across 8 stores when it was first measured.

The repair is an exact inverse of the corruption (encode cp1252, decode utf-8),
verified round-trip, which is why it is safe to apply in bulk and why entries
must NOT be hand-edited instead: an approximate fix of a rationale reads as
correct and is worse than legible damage.

Imports nothing. Used by the quality checks and by the repair handler.
"""



# ============================================================
# Mojibake detection and repair
#
# con-008-dc30 stopped the CAUSE: stdin defaulted to cp1252 on Windows, so a
# client's raw UTF-8 bytes were mis-decoded before json.loads ever ran, and an
# em-dash landed in the store as three characters. Forcing UTF-8 on the stdio
# transport means no new entry is corrupted.
#
# It did nothing for entries already written. A fix that stops the bleeding
# and leaves the wound is half a fix, and the damage is invisible in exactly
# the place it matters: the text is still readable enough that nobody
# re-reads it, so a corrupted rationale just quietly degrades every future
# retrieval that surfaces it.
# ============================================================

# Sequences that only occur when UTF-8 bytes were decoded as cp1252. Requiring
# one of these before attempting a repair keeps the round-trip check below
# from "fixing" text that merely happens to survive the transform.
# Two-and-three character sequences that only ever arise from reading UTF-8 as
# cp1252. Every one begins with Ã / Â / â, because that is what the leading
# byte of a multi-byte UTF-8 sequence becomes when mis-decoded -- which is also
# why a bare em-dash or a bare multiplication sign is NOT listed: those are
# legitimate characters, and matching them would flag correct text as damaged.
#
# The list was incomplete, and silently so. The sequence for a multiplication
# sign was missing, so eleven entries across two stores read "-10 <mojibake>
# min(...)" and neither verify_quality nor repair_mojibake could see them:
# looks_like_mojibake GATES the repair, so a marker this list lacks is a field
# demojibake never even attempts. dec-020 healed 160 entries and left these
# behind. Found 2026-08-05 by a second detector disagreeing with this one.
_MOJIBAKE_MARKERS = (
    "â€",        # em/en-dash and smart-quote family
    "Ã©",        # accented latin: e-acute
    "Ã¨",        # e-grave
    "Ã¼",        # u-umlaut
    "Ã±",        # n-tilde
    "Ã ",        # a-grave
    "Ã´",        # o-circumflex
    "Ã¶",        # o-umlaut
    "Ã¤",        # a-umlaut
    "Ã—",        # multiplication sign
    "Ã·",        # division sign
    "â„",        # trademark / numero
    "âˆ",        # maths
    "Â ",        # non-breaking space
    "Â·",        # middle dot
    "Â«",        # guillemets
    "Â»",
    "Â°",        # degree
    "Â±",        # plus-minus
)


def looks_like_mojibake(text):
    """Cheap pre-filter: does this string carry a known misdecode signature?"""
    return isinstance(text, str) and any(m in text for m in _MOJIBAKE_MARKERS)


def demojibake(text):
    """Repair cp1252-misdecoded UTF-8, or return None if that isn't what this is.

    The repair is the exact inverse of the corruption, so it is verified as
    one: re-applying the corruption to the candidate must reproduce the
    input byte for byte. Anything that fails that check is left alone —
    a partial or approximate repair of someone's recorded reasoning is
    worse than leaving it legibly broken, because it looks fixed.
    """
    if not looks_like_mojibake(text):
        return None
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if repaired == text:
        return None
    try:
        if repaired.encode("utf-8").decode("cp1252") != text:
            return None
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return repaired


# Entry fields that hold prose and are therefore worth repairing. Ids, tags
# and timestamps are excluded: they are machine-facing, and rewriting an id
# would break every related_to pointing at it.
_MOJIBAKE_FIELDS = (
    "summary", "problem", "why_chosen", "what_we_tried", "tradeoffs",
    "rationale", "rule", "reason", "triggering_incident", "purpose",
    "when_to_invoke", "name", "deprecated_reason",
)


def _entry_mojibake_fields(entry):
    """Names of this entry's fields that carry repairable mojibake."""
    hits = []
    for field in _MOJIBAKE_FIELDS:
        if demojibake(entry.get(field)) is not None:
            hits.append(field)
    for i, alt in enumerate(entry.get("alternatives") or []):
        if not isinstance(alt, dict):
            continue
        for key in ("option", "reason_rejected"):
            if demojibake(alt.get(key)) is not None:
                hits.append(f"alternatives[{i}].{key}")
    return hits


def _repair_entry_mojibake(entry):
    """Repair in place. Returns the list of field names changed."""
    changed = []
    for field in _MOJIBAKE_FIELDS:
        fixed = demojibake(entry.get(field))
        if fixed is not None:
            entry[field] = fixed
            changed.append(field)
    for i, alt in enumerate(entry.get("alternatives") or []):
        if not isinstance(alt, dict):
            continue
        for key in ("option", "reason_rejected"):
            fixed = demojibake(alt.get(key))
            if fixed is not None:
                alt[key] = fixed
                changed.append(f"alternatives[{i}].{key}")
    return changed


# Public aliases; server re-exports the underscored names for back-compat.
entry_fields = _entry_mojibake_fields
repair_entry = _repair_entry_mojibake
