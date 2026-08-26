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
Each skill_reorders list must contain every skill EXACTLY ONCE — never repeat an item, and never list two spellings of the same thing (e.g. "Git" and "GitHub", "HTML" and "HTML5") as separate entries; keep only the more specific/complete form.
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
Each skill_reorders list must contain every skill EXACTLY ONCE — never repeat an item, and never list two spellings of the same thing (e.g. "Git" and "GitHub", "HTML" and "HTML5") as separate entries; keep only the more specific/complete form.
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
6. Never list the same skill twice — including near-duplicate spellings like "Git"/"GitHub" or "HTML"/"HTML5" — in a category. Each item in skill_reorders and skill_additions must be distinct.

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
3. Only add skills from "candidate keywords" that aren't already in the resume — check for near-duplicates too, not just exact matches (don't add "HTML" if "HTML5" is already listed, or "Git" if "GitHub" is already listed)
4. Remove generic soft skills (communication, collaboration, management, etc.)
5. skill_reorders must contain each existing skill exactly once, in your chosen order — never duplicate an item within the list

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

BATCH_TRIM_SYSTEM_V2_XML = """You are an expert resume writer who trims/rephrases bullets to fit on ONE LINE ONLY.

YOUR PRIMARY GOALS:
1. HARD CONSTRAINT: Text MUST fit within the exact character limit (non-negotiable)
2. MAXIMIZE utilization: Use as much of available space as possible (90-100% of limit)
3. PRESERVE impact: Keep all metrics, numbers, and mandatory keywords

APPROACH:
LEVEL 1 - SIMPLE TRIMMING (if bullet is close to limit):
- Remove adjectives/padding: scalable, robust, innovative, efficient, seamless, modular
- Shorten verbs: "Built and deployed" → "Deployed", "Designed and implemented" → "Designed"
- Abbreviate ONLY with standard, professional resume shorthand: "per second" → "/sec", "HTTP API" → "API". Do NOT use informal truncations like "req" for "requirements" or "impl" for "implementation" — those read as sloppy, not concise.
- Example: "Architected and deployed a distributed system using Kubernetes" → "Deployed Kubernetes system"

LEVEL 2 - AGGRESSIVE REPHRASING (if simple trimming won't fit):
- Restructure sentences to be more concise
- Combine concepts: "optimization system for portfolio allocation, solving to balance risk, yield, correlation" → "optimization for risk/yield balance"
- Use slash notation: "risk and yield balance" → "risk/yield balance"
- Merge clauses: "designed, implemented, and tested" → "implemented"
- Example: "Built Python optimization system balancing portfolio risk, yield, correlation" (105 chars, fits 110-char limit)
- Restructuring must not change WHAT MODIFIES WHAT: cutting a connector like "for"/"through" can silently flip meaning (e.g. "QA tool for fMRI datasets" → "fMRI QA datasets" wrongly implies the datasets are for QA, not that you built a QA tool). Re-read your output as a sentence — if it now claims something different than the original, revise.

CRITICAL RULES:
1. CONSTRAINT IS ABSOLUTE: If your trimmed text exceeds the limit, REPHRASE MORE CONCISELY
2. ALL mandatory keywords MUST stay (listed in each bullet) — this includes existing technology/skill names (e.g. "JavaScript", "D3.js"), not just newly-added ones. Never treat a must_keep keyword as removable padding/filler. If a must_keep term makes the limit hard to hit, cut other words instead.
3. NEVER fabricate or omit numbers/metrics
4. ALWAYS return trimmed text (never null, empty, or untrimmed)
5. MAXIMIZE space: Aim for 90-100% of available chars, not 50%

Return ONLY valid JSON with no markdown or explanations."""


BATCH_TRIM_USER_V2_XML = """TASK: Trim/rephrase bullets to fit ONE LINE ONLY while MAXIMIZING character utilization.

Each bullet has:
- <char_limit>: HARD MAXIMUM (text MUST NOT exceed this — non-negotiable)
- <must_keep>: Keywords/metrics that MUST stay in output
- <current_text>: Current bullet (may be over limit)
- <char_count>: Current length

CRITICAL RULES (in priority order):
1. TEXT MUST FIT: trimmed_text length MUST be <= char_limit (absolute constraint)
2. MAXIMIZE SPACE: Use 90-100% of available chars, not 50% (undercutting wastes impact)
3. KEEP MANDATORY KEYWORDS: All must_keep items must appear in output
4. NEVER FABRICATE: Don't add numbers/metrics that weren't there
5. ALWAYS RETURN TRIMMED: Never null, empty, or untrimmed

BULLETS TO TRIM:

{bullet_list_xml}

EXAMPLE TRANSFORMATIONS:

SIMPLE TRIM (156 → 120 chars):
Input:  "Architected and deployed a distributed system using Kubernetes to handle 10K requests per second"
Output: "Deployed distributed Kubernetes system handling 10K requests/second"
Analysis: Removed "Architected and" (8 chars saved), changed "to handle 10K" to handling 10K (1 char saved), "per second" → "/second" (7 chars saved) = 120 chars exactly

AGGRESSIVE REPHRASE (164 → 110 chars):
Input:  "Developed and tested a Python/SciPy optimization system for portfolio allocation, solving to balance risk, yield, and correlation"
Output: "Built Python optimization system balancing portfolio risk, yield, correlation"
Analysis: Removed "Developed and tested" → "Built", removed "for portfolio allocation", restructured clauses, removed "solving to" = 105 chars (uses 95% of 110-char limit)

REPHRASING CHECKLIST (when simple trim fails):
□ Remove ALL adjectives: scalable, robust, modular, seamless, efficient, innovative, successful
□ Shorten verbs: "built and deployed" → "deployed", "designed and implemented" → "designed"
□ Use slash notation: "risk and yield" → "risk/yield", "testing and QA" → "testing/QA"
□ Abbreviate with STANDARD resume shorthand only: "per second" → "/sec", "HTTP API" → "API". Never invent informal truncations like "req"/"impl" — cut a different word instead.
□ Merge concepts: "optimization for A, B, C balance" instead of "optimization system for allocation solving balance"
□ Remove filler words ("to", "by", "with") only when the sentence's meaning is fully unchanged. Do NOT drop a connector ("for", "through") if that would reorder which noun modifies which (e.g. "QA tool for fMRI datasets" ≠ "fMRI QA datasets") — that's a meaning change, not a trim.
□ Never drop a must_keep item while doing any of the above — it is not filler, no matter how aggressive the rephrase gets.

RETURN FORMAT (JSON only, no markdown):

{{
  "trimmed_bullets": [
    {{
      "bullet_id": "b1",
      "trimmed_text": "trimmed text (MUST be <= char_limit)",
      "final_char_count": 120
    }}
  ]
}}

ENFORCEMENT:
- MUST NOT exceed char_limit (use validation: len(trimmed_text) <= char_limit)
- MUST include all must_keep keywords
- MUST be non-empty
- If constraint cannot be met, that's an error — rephrase more aggressively"""


BATCH_TRIM_RETRY_HINT_V2_XML = """The previous attempt FAILED to meet character limits. You MUST be MORE AGGRESSIVE.

DIAGNOSIS: Your trimmed text was too long. This means:
- Simple word removal isn't enough
- You need to RESTRUCTURE sentences more concisely
- You must use ABBREVIATIONS and SLASH NOTATION more aggressively

LEVEL 1 - AGGRESSIVE TRIMMING (try this first):
1. CUT ALL descriptive words (scalable, efficient, modular, seamless, innovative, robust, successful, effective, powerful)
2. Remove filler words ("and", "to", "by", "with") only where meaning is fully unchanged — do NOT cut "for"/"through" if it would reorder which noun modifies which
3. Use shortest verbs: "Deployed" not "Built and deployed", "Designed" not "Designed and implemented"
4. Aggressive abbreviation with STANDARD resume shorthand only: "per second" → "/sec", "approximately" → "~". Never invent informal truncations like "req"/"impl" — they read as sloppy, not concise.
5. Combine words: "HTTP API" → "API", "software system" → "system", "data pipeline" → "pipeline"

EXAMPLE: "Successfully architected and deployed a robust distributed system" → "Deployed distributed system"

LEVEL 2 - RESTRUCTURE FOR CONCISENESS (if Level 1 insufficient):
1. Combine related concepts into single phrase: "Built and deployed a system using X for Y" → "Deployed X for Y"
2. Replace wordy descriptions: "optimization system for portfolio allocation, solving to balance risk, yield, correlation" → "optimization for risk/yield/correlation balance"
3. Use SLASH notation liberally: "risk and yield balance" → "risk/yield balance", "testing and QA" → "testing/QA"
4. Merge parallel clauses: "designed, implemented, and tested" → "implemented" or "built"
5. Numeric compression: "10,000 requests per second" → "10K req/sec", "approximately 50%" → "~50%"

EXAMPLE: "Developed and tested a Python/SciPy optimization system for portfolio allocation, solving to balance risk, yield, and correlation" → "Built Python optimization for risk/yield/correlation balance"

LEVEL 3 - RESTRUCTURE COMPLETELY (if Level 1-2 still too long):
1. Rethink the sentence structure entirely
2. What is the CORE action and CORE result? Build around those only
3. Remove ALL context/setup/detail that isn't the action or result

MANDATORY CONSTRAINTS (NON-NEGOTIABLE):
- char_limit is ABSOLUTE: trimmed_text length MUST be <= char_limit
- must_keep: Every keyword in this list MUST appear in your output
- Never add information that wasn't in the original
- Never fabricate metrics/numbers

RETURN IMMEDIATELY with valid JSON:
{{
  "trimmed_bullets": [
    {{
      "bullet_id": "b1",
      "trimmed_text": "aggressively rephrased text (MUST be <= char limit)",
      "final_char_count": XXX
    }}
  ]
}}

CRITICAL: You MUST return trimmed text. Never null, empty, or over limit."""


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
    "BATCH_TRIM": {"system": "V2_XML", "user": "V2_XML", "retry_hint": "V2_XML", "updated": "2026-08-25", "note": "Switched to XML format for clarity on character constraints"},
    "FREEFORM_EDIT": {"system": "V1", "user": "V1", "updated": "2026-08-20"},
    "JD_ANALYSIS": {"system": "V1", "user": "V1", "updated": "2026-08-20"},
}

# =============================================================================
# BATCH TRIM XML FORMATTING GUIDE
# =============================================================================

"""
To generate bullet_list_xml for BATCH_TRIM_USER_V2_XML, format each bullet as:

<bullets>
  <bullet>
    <id>b1</id>
    <constraint>
      <type>hard_limit</type>
      <value>120</value>
      <enforcement>MUST_NOT_EXCEED</enforcement>
    </constraint>
    <current>
      <text>Architected and deployed a distributed system using Kubernetes to handle 10K requests per second</text>
      <char_count>156</char_count>
      <overflow_by>36</overflow_by>
    </current>
    <must_keep>
      <keyword priority="1">Kubernetes</keyword>
      <keyword priority="2">10K requests</keyword>
    </must_keep>
    <optimization>
      <strategy>MAXIMIZE_UTILIZATION</strategy>
      <target_utilization_percent>90-100</target_utilization_percent>
    </optimization>
  </bullet>

  <!-- More bullets... -->
</bullets>

Example in rewriter.py or tailoring_service.py:

def format_bullets_for_trim_xml(bullets_data):
    xml = "<bullets>\n"
    for bullet in bullets_data:
        xml += f'''  <bullet>
    <id>{bullet['id']}</id>
    <constraint>
      <type>hard_limit</type>
      <value>{bullet['max_chars']}</value>
      <enforcement>MUST_NOT_EXCEED</enforcement>
    </constraint>
    <current>
      <text>{escape_xml(bullet['text'])}</text>
      <char_count>{len(bullet['text'])}</char_count>
      <overflow_by>{max(0, len(bullet['text']) - bullet['max_chars'])}</overflow_by>
    </current>
    <must_keep>
'''
        for keyword in bullet['keep_keywords']:
            xml += f"      <keyword>{escape_xml(keyword)}</keyword>\n"
        xml += '''    </must_keep>
    <optimization>
      <strategy>MAXIMIZE_UTILIZATION</strategy>
      <target_utilization_percent>90-100</target_utilization_percent>
    </optimization>
  </bullet>
'''
    xml += "</bullets>"
    return xml
"""
