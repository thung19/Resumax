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

# Known variant relationships: (less_specific, more_specific).
# When both forms are present in the same list, the less specific one is
# dropped and the more specific/canonical one is kept.
VARIANT_PAIRS: list[tuple[str, str]] = [
    ("git", "github"),          # github is more specific
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
    """Lowercase and strip punctuation/whitespace for loose comparison.

    Catches spacing/punctuation-only variants like "Node.js" vs "nodejs"
    or "CI/CD" vs "CI CD" without merging unrelated skills that merely
    share a substring (e.g. "React" and "React Native" stay distinct).
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


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

    # Step 2: drop the less-specific side of any known variant pair
    # (e.g. "Git" when "GitHub" is also present).
    redundant = find_redundant_variants(exact_deduped)
    variant_deduped = (
        [n for n in exact_deduped if n.lower() not in redundant]
        if redundant else exact_deduped
    )

    # Step 3: collapse any remaining punctuation/spacing-only duplicates
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

    redundant = find_redundant_variants(list(existing) + [candidate])
    return candidate_lower in redundant
