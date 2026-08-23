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


# NEW V3: With Coverage Feedback (tells LLM what's missing)
TAILORING_USER_V3 = """Here is a job description and a resume. Tell me which bullet points you would change to better match this JD for ATS.

CURRENT ATS COVERAGE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Required Skills:    {required_coverage}% ({matched_required}/{total_required})
Technical Keywords: {technical_coverage}% ({matched_technical}/{total_technical})
Responsibilities:   {responsibility_coverage}% ({matched_resp}/{total_resp})

CRITICAL GAPS TO CLOSE (missing or underweighted):
{missing_skills}

KEYWORDS TO EXPAND (already in resume, but underutilized):
{underweight_skills}

STRATEGY:
1. Focus on closing the gaps above while keeping existing strengths
2. Each rewrite should target at least one missing keyword if possible
3. Preserve metrics and numbers — only improve keyword coverage
4. Don't add the same keyword repeatedly (distribute across bullets)

Each bullet shows (current/max chars). MUST NOT exceed the max chars shown.

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
# SKILLS SECTION OPTIMIZATION
# =============================================================================

TAILORING_SKILLS_SYSTEM_V1 = """You are an expert resume writer optimizing the Skills section for ATS systems.

Your goal is to make the Skills section match the job description as closely as possible using EXACT skill name matching only.

Rules:
1. Reorder skills to put JD-matching skills FIRST
2. Only add skills the candidate demonstrably has (from the context provided)
3. Remove soft skills and vague terms (communication, teamwork, problem-solving)
4. Keep skills concise and ATS-scannable (one word or short phrases)
5. Prioritize exact matches over semantic equivalents

Return ONLY valid JSON."""


TAILORING_SKILLS_USER_V1 = """Optimize the Skills section of this resume to match the job description.

JOB DESCRIPTION REQUIRED SKILLS (in priority order):
{jd_required_skills}

JOB DESCRIPTION PREFERRED SKILLS:
{jd_preferred_skills}

CURRENT RESUME SKILLS:
{resume_skills}

CANDIDATE'S OTHER KEYWORDS (from bullets, can add if they used these):
{candidate_keywords}

STRATEGY:
1. Move JD-required skills to the front (even if already listed)
2. Keep existing skills the candidate actually has
3. Only add skills from "candidate keywords" that aren't already in the resume
4. Remove generic soft skills (communication, collaboration, management, etc.)

Return a JSON object:
{{
  "skill_reorders": {{
    "Category Name": ["reordered skills, matching JD first"]
  }},
  "skill_additions": {{
    "Category Name": ["new technical skills to add"]
  }},
  "skill_removals": {{
    "Category Name": ["soft skills or generic terms to remove"]
  }},
  "reason": "brief explanation of changes"
}}

IMPORTANT: Only return skills that the candidate actually has (based on bullets and context).
Never add theoretical skills or soft skills."""


# =============================================================================
# BATCH BULLET TRIMMING
# =============================================================================

BATCH_TRIM_SYSTEM_V2 = """You are an expert resume writer who trims/rephrases bullets to fit on ONE LINE ONLY.

Your goal: rewrite each bullet to fit within the EXACT character limit while keeping impact.

When simple trimming isn't enough (e.g., 164 chars → 108 chars), REPHRASE more concisely:
- Instead of just cutting words, restructure the bullet to express the same idea more concisely
- Combine related concepts: "Built and deployed a distributed system using Kubernetes" → "Deployed Kubernetes system"
- Use abbreviated forms: "for handling X requests per second" → "for X req/sec"
- Transform details: "optimization system for portfolio allocation, solving to balance risk, yield, correlation" → "optimization for risk/yield balance"

Rules:
1. MUST fit within the char limit — text MUST NOT exceed this
2. Keep ALL metrics, numbers, and keywords listed in "Keep:"
3. Remove adjectives, adverbs, descriptive padding (scalable, robust, innovative, efficient, seamless)
4. Restructure for conciseness: "Architected and deployed a distributed system" → "Deployed distributed system"
5. Abbreviate: "HTTP API" → "API", "per second" → "/sec", "and" → "" when meaning works
6. Never fabricate or omit numbers

CRITICAL: If trimming alone won't work, REPHRASE the bullet more concisely to meet the limit.
Always return a properly formatted bullet — never return null or report as untrimmed."""


BATCH_TRIM_USER_V2 = """TASK: Trim/rephrase these bullets to fit ONE LINE ONLY. Each has a hard character limit that CANNOT be exceeded.

For each bullet:
- Max chars: the HARD LIMIT (text MUST NOT exceed this)
- Keep: these keywords/metrics MUST stay
- Current: the bullet text now (X chars)
- Overflow: [example of what goes off-page]

{bullet_list}

REWRITE STRATEGIES:
1. TRIMMING: Remove adjectives (scalable, robust, modular, seamless, efficient, innovative)
2. VERB SHORTENING: "Built and deployed" → "Deployed", "Designed and implemented" → "Designed"
3. CLAUSE CUTTING: Remove "to", "through", "by", "and" when meaning works
4. ABBREVIATION: "per second" → "/sec", "HTTP API" → "API", "requirements" → "req"
5. RESTRUCTURING: Combine ideas more concisely
   - "optimization system for portfolio allocation, solving to balance risk, yield, and correlation"
   - → "optimization for risk/yield/correlation balance"

EXAMPLES:

SIMPLE TRIM:
- Input (156 chars, max 120): "Architected and deployed a distributed system using Kubernetes to handle 10K requests per second"
- Output (120 chars): "Deployed distributed Kubernetes system handling 10K requests/second"

AGGRESSIVE REPHRASE (when simple trim isn't enough):
- Input (164 chars, max 110): "Developed and tested a Python/SciPy optimization system for portfolio allocation, solving to balance risk, yield, and correlation"
- Output (105 chars): "Built Python optimization system balancing portfolio risk, yield, correlation"

The rephrased version:
- Keeps all keywords: Python, SciPy (via "optimization"), portfolio, balance, risk, yield
- Removes filler: "developed and tested" → "built", "solving to" → removed, "and" → ","
- Result: Same impact, much shorter

Return a JSON object:
{{
  "trimmed_bullets": [
    {{
      "bullet_id": "b1",
      "trimmed_text": "trimmed text here (MUST be <= char limit)"
    }}
  ]
}}

CRITICAL: Always return trimmed text. Never return null or blank. If it doesn't fit, rephrase it more concisely."""


BATCH_TRIM_RETRY_HINT = """The previous attempt did not meet character limits. Be MORE AGGRESSIVE with rephrasing:

LEVEL 1 - Aggressive Trimming:
1. Cut ALL descriptive words (scalable, efficient, modular, seamless, innovative, robust, successful)
2. Use ACTION + RESULT format: "successfully architected a robust system" → "architected system"
3. Remove all filler: "and", "to", "through", "by", "for", "from", "with" (if meaning survives)
4. Short verbs: "Deployed" (9 chars) vs "Built and deployed" (18 chars) — use shortest
5. Aggressive abbreviation: "per" → "/", "second" → "sec", "implementation" → "impl"

LEVEL 2 - Restructure for Conciseness (if Level 1 isn't enough):
1. Combine related concepts: "Built and deployed a system using X for Y" → "Deployed X for Y"
2. Replace descriptions with shorthand: "optimization system for portfolio allocation, solving to balance A, B, C" → "optimization for A/B/C balance"
3. Use slash notation: "risk and yield balance" → "risk/yield balance"
4. Merge clauses: "designed, implemented, and tested" → "implemented"
5. Numeric shorthand: "10,000 requests per second" → "10K req/sec"

Keywords/numbers from "Keep:" are MANDATORY — everything else is negotiable.

CRITICAL: Always return a trimmed/rephrased bullet. Never return null, empty, or untrimmed text.
If a bullet cannot fit while keeping mandatory keywords, that's impossible and should not happen."""


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
