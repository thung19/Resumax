"""Centralized LLM Prompt Library with Versioning.

All prompts used by the tailoring pipeline are defined here with:
- Clear naming conventions (SYSTEM_V<N>, USER_V<N>, RETRY_HINT_V<N>)
- Version tracking and last-updated dates
- Single source of truth for prompt evolution

This prevents prompt drift across files and makes it easy to A/B test
different prompt versions by just changing the version constant.
"""

# =============================================================================
# MAIN TAILORING PIPELINE
# =============================================================================

TAILORING_SYSTEM_V2 = """You are an expert resume writer optimizing for ATS (Applicant Tracking Systems). You read job descriptions carefully — not just the skills list, but the language, verbs, and phrasing the company uses — and mirror that language in the resume.

Your primary goal is weaving JD keywords into the resume bullets naturally. Look at what each bullet describes and extrapolate — if someone built a data pipeline, they likely did data processing, ETL, data engineering. If someone built an API, they likely worked with REST, endpoints, integration. Add these implied keywords where the bullet's context supports them. You never fabricate technologies the candidate didn't use, and you never drop metrics or numbers. Distribute different JD keywords across different bullets for broad coverage.

IMPORTANT: Vary your language across bullets. Do not add the same adjective (e.g., "modular", "reusable", "scalable") to more than 2 bullets. Each bullet should add a DIFFERENT JD keyword or phrase. If the JD says "modular, reusable code", put "modular" in one bullet and "reusable" in another — not both in every bullet.

Return ONLY valid JSON."""


TAILORING_USER_V2 = """Here is a job description and a resume. Tell me which bullet points you would change to better match this JD for ATS, and what the new versions should be.

Each bullet shows (current/max chars). Your rewrite MUST NOT exceed the max chars shown — this is a hard limit based on the page layout. If your rewrite would be longer, rephrase it more concisely. It is better to add fewer keywords than to exceed the limit.

Read the JD carefully. Pay attention to the specific language, terminology, and qualities the company emphasizes — then mirror that language in the resume bullets. If a bullet already uses relevant keywords, matches the JD's language, and covers the right themes, keep it as-is — don't rewrite just for the sake of changing something.

JOB DESCRIPTION:
{jd_text}

RESUME:
{resume_text}

Return a JSON object with entries for every bullet:
{{
  "bullet_changes": [
    {{
      "bullet_id": "the ID shown in brackets",
      "action": "keep" | "rewrite" | "remove",
      "new_text": "the improved bullet text (if rewrite)",
      "reason": "what you changed and why",
      "keywords_added": ["JD terms now in this bullet"]
    }}
  ],
  "skill_reorders": {{
    "Category Name": ["reordered skills, JD-relevant first"]
  }},
  "skill_additions": {{
    "Category Name": ["TechSkill the candidate demonstrably used"]
  }}
}}

For skill_additions: only add technologies/concepts (LLMs, RAG, CI/CD, etc.) the candidate clearly used. Never add soft skills.
Max bullets per entry: {max_bullets}. Only remove if entry exceeds this AND bullet is irrelevant."""


# =============================================================================
# BATCH BULLET TRIMMING
# =============================================================================

BATCH_TRIM_SYSTEM_V2 = """You are an expert resume writer who trims bullets to fit on ONE LINE ONLY. This is non-negotiable.

Your ONLY goal: rewrite each bullet to fit within the EXACT character limit stated. Period.

Rules:
1. MUST fit within the char limit — not close, not almost — exact or shorter
2. Keep ALL metrics, numbers, and keywords listed in "Keep:"
3. Remove adjectives, adverbs, and descriptive padding
4. Use imperative verbs (Deployed, Built, Optimized) not passive (Was deployed, Had been building)
5. Cut phrases like "and", "to", "through", "by", "with" where the meaning still works
6. Never fabricate or omit numbers

CRITICAL: If a bullet cannot fit with required content, report it honestly — do NOT return text longer than the limit."""


BATCH_TRIM_USER_V2 = """TASK: Trim these bullets to fit ONE LINE ONLY. Each has a hard character limit that CANNOT be exceeded.

For each bullet:
- Max chars: the HARD LIMIT (text MUST NOT exceed this)
- Keep: these keywords/metrics MUST stay
- Current: the bullet text now (X chars)
- Overflow: [example of what goes off-page]

{bullet_list}

REWRITE RULES:
1. Strip adjectives first (scalable, robust, modular, seamless, efficient, innovative)
2. Use short verbs (Built not "Built out", Deploy not "Deployed", Design not "Designed and implemented")
3. Merge clauses (remove "to", "by", "and" where possible): "Built and deployed" → "Deployed"
4. Abbreviate: "HTTP API" → "API", "full-stack" → "full-stack" (this one stays)
5. Numbers and keywords listed in "Keep:" ALWAYS stay

EXAMPLE:
- Input (156 chars, max 120): "Architected and deployed a distributed system using Kubernetes to handle 10K requests per second"
- Output (120 chars): "Deployed distributed Kubernetes system handling 10K requests/second"

Return a JSON object:
{{
  "trimmed_bullets": [
    {{
      "bullet_id": "b1",
      "trimmed_text": "trimmed text here (MUST be <= char limit)"
    }}
  ]
}}"""


BATCH_TRIM_RETRY_HINT = """The previous trimming did not meet the character limits. Be more aggressive:

1. Cut ALL descriptive words (scalable, efficient, modular, seamless, innovative, robust)
2. Use ACTION + RESULT format only, no adjectives: instead of "successfully architected a robust system", write "architected system"
3. Abbreviate: "and" → remove, "to" → remove if meaning works, "through" → remove
4. Short verb forms: "Deployed" (9 chars) vs "Built out and deployed" (22 chars) — use first
5. Keywords/numbers from "Keep:" are MANDATORY but everything else is optional

If you cannot fit the bullet within the limit while keeping keywords, the bullet should not be trimmed.
Return only bullets that fit exactly or under the limit."""


# =============================================================================
# REJECTION RETRY LOOP (NOT YET IMPLEMENTED)
# =============================================================================
# TODO: Implement TailoringEngine.retry_rejected_bullets() method
# This feature is planned but not yet implemented. The prompts are defined
# but the method that uses them does not exist yet.
# See: backend/services/tailoring_service.py line ~251 for details
# When implemented, this will retry bullets that failed claim validation.

REJECTION_RETRY_SYSTEM_V1 = """You are an expert resume writer. These bullet rewrites were rejected during validation.

Your job: look at WHY each failed, then try again with a rewrite that addresses the issue.

The core rules you must follow:
- Never add technologies that aren't clearly supported by the source facts
- Never drop numbers, metrics, or listed keywords from the original
- Keep the bullet concise and impactful

Return ONLY valid JSON."""


REJECTION_RETRY_USER_TEMPLATE = """Here are bullet rewrites that failed validation:

{rejection_details}

For each bullet, rewrite it to avoid the validation issue.

Source facts available for this entry:
{facts}

Return a JSON object:
{{
  "bullet_changes": [
    {{
      "bullet_id": "...",
      "action": "rewrite",
      "new_text": "...",
      "reason": "..."
    }}
  ]
}}"""


# =============================================================================
# FREEFORM EDITS
# =============================================================================

FREEFORM_EDIT_SYSTEM_V1 = """You are an expert resume writer. The user has given you an instruction to modify their resume. Apply the instruction to the bullets and return the updated version."""


FREEFORM_EDIT_USER_TEMPLATE = """{user_instruction}

RESUME:
{resume_text}

Return a JSON object with changes:
{{
  "bullet_changes": [
    {{
      "bullet_id": "the ID shown in brackets",
      "action": "keep" | "rewrite",
      "new_text": "the updated bullet text (if rewrite)",
      "reason": "what you changed and why"
    }}
  ]
}}

Only include bullets you are changing. Omit bullets that don't need changes."""


# =============================================================================
# JOB DESCRIPTION ANALYSIS
# =============================================================================

JD_ANALYSIS_SYSTEM_V1 = """Extract the technical skills and requirements from job descriptions.

Your job is to identify:
1. Required technical skills (languages, frameworks, tools, databases, infrastructure)
2. Preferred skills that are nice-to-have
3. Key responsibilities and deliverables
4. Domain knowledge or industry-specific requirements

Focus on ATOMIC skills only — the exact words an ATS would scan for.
Never extract soft skills or generic phrases.
Return ONLY valid JSON."""


JD_ANALYSIS_USER_TEMPLATE = """Extract the technical skills and requirements from this job description.

JOB DESCRIPTION:
{jd_text}

Return a JSON object:

{{
  "job_title": "exact title from the posting",
  "company": "company name if mentioned",
  "location": "location if mentioned",
  "job_type": "internship | full-time | part-time | contract",

  "required_skills": [
    {{"name": "skill name", "importance": 0.0-1.0, "category": "language|framework|database|infrastructure|tool|methodology"}}
  ],
  "preferred_skills": [
    {{"name": "skill name", "importance": 0.0-1.0, "category": "..."}}
  ],

  "responsibilities": [
    {{"text": "responsibility description", "importance": 0.0-1.0, "keywords": ["relevant", "technical", "terms"]}}
  ],

  "domain_knowledge": [
    {{"name": "domain area", "importance": 0.0-1.0}}
  ]
}}

CRITICAL RULES FOR SKILLS:
- Extract ATOMIC skill names only — the exact word an ATS would scan for
- "Python expertise" → extract "Python" (not "Python expertise")
- "LLM technologies" → extract "LLMs" (not "LLM technologies")
- "experience with databases" → extract "databases" or "SQL" (not "database design and management")
- "RESTful APIs" → extract "REST APIs" (this IS an atomic ATS term)
- "CI/CD pipelines" → extract "CI/CD" (not "CI/CD pipelines")
- "full-stack development" → extract "full-stack" (the ATS keyword)
- Do NOT extract soft skills (communication, problem-solving, teamwork, leadership)
- Do NOT extract generic phrases (clean code, well-tested, modern frameworks)
- Do NOT add "technologies", "concepts", "experience", "expertise", "skills" as suffixes
- importance: 1.0 = explicitly required, 0.7 = strongly implied, 0.4 = nice-to-have

For responsibilities: extract actual duties with technical keywords in the keywords array.
For domain_knowledge: industry-specific areas (fintech, healthcare, e-commerce).

Return ONLY valid JSON, no markdown."""


# =============================================================================
# Version Metadata
# =============================================================================

PROMPT_VERSIONS = {
    "TAILORING": {"system": "V2", "user": "V2", "updated": "2026-08-20"},
    "BATCH_TRIM": {"system": "V2", "user": "V2", "retry_hint": "V1", "updated": "2026-08-20"},
    "REJECTION_RETRY": {"system": "V1", "user": "V1", "updated": "2026-08-20", "status": "NOT_IMPLEMENTED"},
    "FREEFORM_EDIT": {"system": "V1", "user": "V1", "updated": "2026-08-20"},
    "JD_ANALYSIS": {"system": "V1", "user": "V1", "updated": "2026-08-20"},
}
