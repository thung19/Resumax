"""Shared skill/keyword deduplication utilities.

Used on both sides of the pipeline:
- JD-side extraction (`job_analyzer.py`) dedupes skills pulled out of a job
  posting (e.g. "Git" and "GitHub" both appearing in the text).
- Resume-side tailoring (`tailoring_service.py`) dedupes skill reorders/
  additions an LLM proposes for the Skills (and Skills & Interests) section,
  so the same item doesn't get listed twice under slightly different
  spelling or punctuation.

Keeping this in one place avoids the two sides drifting out of sync, which
is what happened previously: variant-aware dedup existed only for JD
extraction, not for merging LLM skill suggestions into the resume.
"""

from __future__ import annotations

import re

# Known variant relationships: (less_specific, more_specific).
# When both forms are present in the same list, the less specific one is
# dropped and the more specific/canonical one is kept.
#
# Unlike every other pair here (a genuine same-technology spelling/version
# variant — "Node.js" vs "nodejs" is the same runtime, "html" vs "html5"
# the same markup language at different specificity), Git and GitHub are
# two different, if related, things: a CLI version-control tool vs. a
# hosting platform built by a different company. A resume — or a JD —
# listing both is plausibly deliberate, and a JD requiring "Git"
# specifically shouldn't have that requirement silently unmatched because
# the resume's "Git" entry got dropped in favor of "GitHub". No pair for
# them here; they're intentionally treated as distinct.
VARIANT_PAIRS: list[tuple[str, str]] = [
    ("node", "node.js"),        # node.js is canonical
    ("nodejs", "node.js"),      # node.js is canonical
    ("vue", "vue.js"),          # vue.js is more complete
    ("vuejs", "vue.js"),        # vue.js is canonical
    ("next", "next.js"),        # next.js is canonical
    ("nextjs", "next.js"),      # next.js is canonical
    ("nest", "nest.js"),        # nest.js is canonical
    ("nestjs", "nest.js"),      # nest.js is canonical
    ("tailwind", "tailwindcss"),  # tailwindcss is more complete
    ("rest", "restful"),        # restful is more specific
    ("html", "html5"),          # html5 is more specific
    ("css", "css3"),            # css3 is more specific
]


def normalize_skill_name(name: str) -> str:
    """Lowercase and strip decorative punctuation/whitespace for loose
    comparison.

    Catches spacing/punctuation-only variants like "Node.js" vs "nodejs"
    or "CI/CD" vs "CI CD" without merging unrelated skills that merely
    share a substring (e.g. "React" and "React Native" stay distinct).

    "+" and "#" are kept rather than stripped: stripping them collapsed
    "C", "C++", and "C#" — three genuinely distinct, extremely common
    languages — into the same normalized "c", so listing more than one
    silently dropped all but one. Unlike the punctuation this strips
    (dots, spaces, slashes — purely decorative formatting differences),
    +/# are semantically significant: they're what makes C# and C++
    different languages from C and from each other, not a spelling
    variant of the same thing.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch in "+#")


def split_combo_skill(name: str) -> list[str]:
    """Split a slash-combined skill entry like "JavaScript/TypeScript" into
    its individual parts. Returns [name] unchanged if there's nothing to
    split (a single part, or a slash that's part of a canonical name like
    "Next.js" — those never contain a "/").
    """
    parts = [p.strip() for p in re.split(r"\s*/\s*", name) if p.strip()]
    return parts if len(parts) > 1 else [name]


def _drop_standalone_covered_by_combo(names: list[str]) -> list[str]:
    """Drop standalone entries already covered by a combo entry's parts.

    E.g. given ["JavaScript/TypeScript", "TypeScript", "JavaScript"], the
    combo already lists both languages, so the two standalone entries are
    redundant and get dropped, leaving just the combo.
    """
    combo_parts_lower: set[str] = set()
    for n in names:
        parts = split_combo_skill(n)
        if len(parts) > 1:
            combo_parts_lower.update(p.lower() for p in parts)

    if not combo_parts_lower:
        return names

    return [
        n for n in names
        if len(split_combo_skill(n)) > 1 or n.lower() not in combo_parts_lower
    ]


def find_redundant_variants(names: list[str]) -> set[str]:
    """Return lowercased names in `names` that are redundant variants
    of another, more specific name also present in the list.

    E.g. given ["Git", "GitHub"], returns {"git"} — the caller should
    drop the item(s) whose lowercased form is in the returned set.

    NOTE: this intentionally compares by plain `.lower()`, not
    `normalize_skill_name()`. Several pairs (e.g. "nodejs"/"node.js")
    normalize to the *same* punctuation-stripped string, so comparing
    normalized forms here would make the pair look "both present" from
    a single surviving entry and wrongly drop it. Punctuation-only
    duplicates are already collapsed by the normalized pass in
    `dedupe_skill_names` before this runs.
    """
    present = {n.lower() for n in names}
    redundant: set[str] = set()
    for less_specific, more_specific in VARIANT_PAIRS:
        if more_specific.lower() in present and less_specific.lower() in present:
            redundant.add(less_specific.lower())
    return redundant


def dedupe_skill_names(names: list[str]) -> list[str]:
    """Remove exact, known-variant, and normalized duplicates from a list
    of skill/interest strings, preserving first-occurrence order and the
    original casing/spelling of the kept item.

    Order of passes matters: known-variant dedup (step 2) needs to see
    both spellings of a pair like "Nextjs"/"Next.js" before a punctuation-
    insensitive collapse (step 3) has a chance to arbitrarily discard one
    of them by whichever appeared first — that would leave a variant
    behind, unmatched by any pair.
    """
    # Step 1: drop exact case-insensitive duplicates.
    exact_deduped: list[str] = []
    seen_exact: set[str] = set()
    for name in names:
        stripped = name.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in seen_exact:
            continue
        seen_exact.add(lowered)
        exact_deduped.append(stripped)

    # Step 2: drop standalone entries already covered by a combo entry's
    # parts (e.g. "TypeScript" and "JavaScript" when "JavaScript/TypeScript"
    # is also present).
    combo_deduped = _drop_standalone_covered_by_combo(exact_deduped)

    # Step 3: drop the less-specific side of any known variant pair
    # (e.g. "Git" when "GitHub" is also present).
    redundant = find_redundant_variants(combo_deduped)
    variant_deduped = (
        [n for n in combo_deduped if n.lower() not in redundant]
        if redundant else combo_deduped
    )

    # Step 4: collapse any remaining punctuation/spacing-only duplicates
    # not covered by an explicit pair (e.g. "CI/CD" vs "CI CD").
    final: list[str] = []
    seen_normalized: set[str] = set()
    for name in variant_deduped:
        norm = normalize_skill_name(name)
        if norm in seen_normalized:
            continue
        seen_normalized.add(norm)
        final.append(name)

    return final


def is_duplicate_skill(candidate: str, existing: list[str]) -> bool:
    """Return True if `candidate` duplicates something already in `existing`
    — exactly, by normalized spelling, or as a known less-specific variant
    (e.g. "HTML" when "HTML5" is already present).

    Use this when deciding whether to append a single new skill/interest
    to a list one at a time (e.g. merging an LLM's suggested addition into
    a resume's existing Skills entries).
    """
    candidate = candidate.strip()
    if not candidate:
        return True

    candidate_lower = candidate.lower()
    candidate_norm = normalize_skill_name(candidate)
    for item in existing:
        if item.lower() == candidate_lower or normalize_skill_name(item) == candidate_norm:
            return True
        # Candidate already covered by a combo entry, e.g. candidate
        # "JavaScript" vs. existing "JavaScript/TypeScript".
        if candidate_lower in {p.lower() for p in split_combo_skill(item)}:
            return True

    # Candidate is itself a combo whose parts are all already present
    # separately, e.g. candidate "JavaScript/TypeScript" vs. existing
    # ["JavaScript", "TypeScript"] — adds nothing new.
    candidate_parts = split_combo_skill(candidate)
    if len(candidate_parts) > 1:
        existing_lower = {e.lower() for e in existing}
        if all(p.lower() in existing_lower for p in candidate_parts):
            return True

    redundant = find_redundant_variants(list(existing) + [candidate])
    return candidate_lower in redundant
