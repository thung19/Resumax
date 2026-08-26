"""Job Description Analyzer — Hybrid LLM + Deterministic.

Pipeline:
1. Deterministic extraction ALWAYS runs first (keyword dictionaries,
   pattern matching, term frequency, section detection)
2. LLM enrichment runs second IF available — Claude analyzes the full JD
   for semantic understanding that rules can't capture:
   - Skills/tools not in our dictionaries
   - Correct required vs preferred classification from ambiguous phrasing
   - Importance weighting based on context and emphasis
   - Domain knowledge and industry-specific terminology
   - Responsibilities from narrative paragraphs (not just bullet lists)
   - Seniority signals and experience level expectations
3. LLM results are MERGED into the deterministic baseline — they add and
   adjust but never delete what deterministic found
4. If LLM fails for any reason, the deterministic result is returned unchanged
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from typing import Optional

from backend.models.job_description import (
    JobAnalysis,
    Responsibility,
    WeightedItem,
    JDRequirement,
    ConcreteDeliverable,
    BehavioralRequirement,
    EducationGate,
    ExperienceGate,
    EligibilityGate,
)
from backend.prompts import JD_ANALYSIS_SYSTEM_V1, JD_ANALYSIS_USER_TEMPLATE
from backend.analysis.skill_dedup import find_redundant_variants

logger = logging.getLogger(__name__)

# --- Known technology/skill dictionaries ---

PROGRAMMING_LANGUAGES = {
    "python", "java", "javascript", "typescript", "c++", "c#", "c",
    "go", "golang", "rust", "ruby", "php", "swift", "kotlin", "scala",
    "r", "matlab", "perl", "lua", "dart", "elixir", "haskell",
    "sql", "html", "css", "bash", "shell", "powershell",
}

FRAMEWORKS = {
    "react", "next.js", "nextjs", "angular", "vue", "vue.js", "vuejs",
    "svelte", "django", "flask", "fastapi", "spring", "spring boot",
    "express", "express.js", "node.js", "nodejs", "nest.js", "nestjs",
    "rails", "ruby on rails", ".net", "asp.net", "laravel",
    "tensorflow", "pytorch", "keras", "scikit-learn", "sklearn",
    "pandas", "numpy", "scipy", "matplotlib", "d3.js", "d3",
    "jquery", "bootstrap", "tailwind", "tailwindcss",
    "react native", "flutter", "electron",
    "graphql", "rest", "restful", "grpc",
    "sentence-transformers", "sentencetransformers", "langchain",
    "machine learning", "deep learning", "neural network", "neural networks",
    "llm", "llms", "large language model", "large language models",
    "embeddings", "embedding", "vector search", "semantic search",
    "nlp", "natural language processing", "computer vision",
    "rag", "retrieval augmented generation",
    "openai", "gemini", "hugging face", "huggingface",
    "full-stack", "full stack", "fullstack",
}

DATABASES = {
    "postgresql", "postgres", "mysql", "mariadb", "sqlite",
    "mongodb", "dynamodb", "cassandra", "redis", "elasticsearch",
    "neo4j", "oracle", "sql server", "mssql",
    "firebase", "firestore", "supabase",
    "pinecone", "weaviate", "chromadb", "milvus",
}

INFRASTRUCTURE = {
    "aws", "amazon web services", "gcp", "google cloud", "azure",
    "docker", "kubernetes", "k8s", "terraform", "ansible",
    "jenkins", "github actions", "gitlab ci", "circleci",
    "nginx", "apache", "linux", "unix",
    "vercel", "netlify", "heroku", "render", "railway",
    "cloudflare", "lambda", "s3", "ec2", "ecs", "fargate",
}

TOOLS = {
    "git", "github", "gitlab", "bitbucket",
    "jira", "confluence", "trello", "asana", "linear",
    "figma", "sketch", "adobe",
    "postman", "swagger", "openapi",
    "webpack", "vite", "babel", "esbuild",
    "jest", "pytest", "junit", "mocha", "cypress", "playwright",
    "datadog", "splunk", "grafana", "prometheus",
    "bloomberg", "bloomberg api",
}

METHODOLOGIES = {
    "agile", "scrum", "kanban", "lean", "waterfall",
    "ci/cd", "ci cd", "continuous integration", "continuous deployment",
    "tdd", "test-driven development", "bdd",
    "devops", "sre", "site reliability",
    "microservices", "monolith", "serverless",
    "oop", "object-oriented", "functional programming",
    "design patterns", "solid", "clean architecture",
    "rest api", "restful api", "api design",
    "data structures", "algorithms",
}

SOFT_SKILLS_PATTERNS = [
    "communication", "teamwork", "collaboration", "leadership",
    "problem.solving", "critical.thinking", "analytical",
    "time.management", "self.motivated", "detail.oriented",
    "attention to detail", "fast.paced", "cross.functional",
    "mentoring", "presentation", "stakeholder",
]



def _normalize(text: str) -> str:
    return text.lower().strip()


def _find_matches(text_lower: str, dictionary: set[str]) -> list[str]:
    matches = []
    for term in sorted(dictionary, key=len, reverse=True):
        if len(term) <= 5:
            # Short terms need word boundaries to avoid substring matches
            # e.g., "lean" matching inside "clean"
            pattern = rf"\b{re.escape(term)}\b"
        else:
            pattern = re.escape(term)
        if re.search(pattern, text_lower, re.IGNORECASE):
            matches.append(term)
    return matches


class JobAnalyzer:
    """Hybrid LLM + deterministic job description analyzer."""

    def __init__(self, use_llm: bool = True, model: str = "claude-haiku-4-5-20251001"):
        self._use_llm = use_llm
        self._model = model
        self._llm_used = False
        self._llm_error: Optional[str] = None

    @property
    def llm_used(self) -> bool:
        return self._llm_used

    @property
    def llm_error(self) -> Optional[str]:
        return self._llm_error

    def analyze(self, jd_text: str) -> JobAnalysis:
        """Run the full hybrid analysis pipeline."""
        # Stage 1: Deterministic (always runs)
        analysis = self._deterministic_analysis(jd_text)

        # Stage 2: LLM enrichment (if available, with full fallback)
        if self._use_llm:
            try:
                self._llm_enrich(jd_text, analysis)
                self._llm_used = True
            except Exception as e:
                import re as _re
                sanitized = _re.sub(r"sk-[a-zA-Z0-9_-]{10,}", "[REDACTED]", str(e))[:200]
                self._llm_error = sanitized
                logger.warning(f"LLM JD analysis failed, using deterministic only: {sanitized}")

        # Stage 3: Categorize requirements for ATS matching
        self._categorize_requirements_for_ats(analysis)

        return analysis

    # ----------------------------------------------------------------
    # STAGE 1: Deterministic
    # ----------------------------------------------------------------

    def _deterministic_analysis(self, jd_text: str) -> JobAnalysis:
        analysis = JobAnalysis(raw_text=jd_text)
        text_lower = _normalize(jd_text)

        self._extract_role_info(jd_text, analysis)
        self._extract_technologies(text_lower, analysis)
        self._extract_responsibilities(jd_text, analysis)
        self._analyze_term_frequency(jd_text, analysis)
        self._extract_soft_skills(text_lower, analysis)
        self._assign_weights(jd_text, analysis)
        self._build_ats_phrases(analysis)

        return analysis

    def _extract_role_info(self, text: str, analysis: JobAnalysis):
        lines = text.strip().split("\n")
        for line in lines[:5]:
            line = line.strip()
            if not line:
                continue
            if any(kw in line.lower() for kw in ["about us", "about the", "company", "overview"]):
                continue
            if not analysis.job_title and len(line) < 100:
                analysis.job_title = line
                break

        if re.search(r"\bintern\b", text, re.I):
            analysis.job_type = "internship"
        elif re.search(r"\bfull.time\b", text, re.I):
            analysis.job_type = "full-time"
        elif re.search(r"\bcontract\b", text, re.I):
            analysis.job_type = "contract"

    def _extract_technologies(self, text_lower: str, analysis: JobAnalysis):
        # Track what we've already added to prevent duplicates
        seen_lower = set()

        for lang in _find_matches(text_lower, PROGRAMMING_LANGUAGES):
            lang_lower = lang.lower()
            if lang_lower not in seen_lower:
                analysis.programming_languages.append(WeightedItem(name=lang, category="language"))
                seen_lower.add(lang_lower)

        for fw in _find_matches(text_lower, FRAMEWORKS):
            fw_lower = fw.lower()
            if fw_lower not in seen_lower:
                analysis.frameworks.append(WeightedItem(name=fw, category="framework"))
                seen_lower.add(fw_lower)

        for db in _find_matches(text_lower, DATABASES):
            db_lower = db.lower()
            if db_lower not in seen_lower:
                analysis.databases.append(WeightedItem(name=db, category="database"))
                seen_lower.add(db_lower)

        for infra in _find_matches(text_lower, INFRASTRUCTURE):
            infra_lower = infra.lower()
            if infra_lower not in seen_lower:
                analysis.infrastructure.append(WeightedItem(name=infra, category="infrastructure"))
                seen_lower.add(infra_lower)

        for tool in _find_matches(text_lower, TOOLS):
            tool_lower = tool.lower()
            if tool_lower not in seen_lower:
                analysis.tools.append(WeightedItem(name=tool, category="tool"))
                seen_lower.add(tool_lower)

        for meth in _find_matches(text_lower, METHODOLOGIES):
            meth_lower = meth.lower()
            if meth_lower not in seen_lower:
                analysis.methodologies.append(WeightedItem(name=meth, category="methodology"))
                seen_lower.add(meth_lower)

        # Deduplicate variant forms: prefer more specific/complete terms
        self._deduplicate_variants(analysis)

    def _deduplicate_variants(self, analysis: JobAnalysis):
        """Remove redundant variant forms, preferring the more specific/complete term.

        Examples of variants to deduplicate:
        - "git" vs "github" → keep "github" (more specific)
        - "node.js" vs "nodejs" → keep "node.js" (canonical form)
        - "vue.js" vs "vue" → keep "vue.js" (more complete)
        - "next.js" vs "nextjs" → keep "next.js" (canonical form)

        Shared with the resume-tailoring side (`skill_dedup.py`), which applies
        the same variant table when merging LLM skill suggestions into a resume.
        """
        for list_obj in [
            analysis.frameworks, analysis.tools, analysis.programming_languages,
            analysis.databases, analysis.infrastructure,
        ]:
            redundant = find_redundant_variants([item.name for item in list_obj])
            if redundant:
                list_obj[:] = [
                    item for item in list_obj
                    if item.name.lower() not in redundant
                ]

    def _extract_responsibilities(self, text: str, analysis: JobAnalysis):
        lines = text.split("\n")
        in_responsibilities = False

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()

            if any(kw in lower for kw in [
                "responsibilit", "what you'll do", "what you will do",
                "duties", "role description", "day-to-day",
                "in this role", "you will", "your role",
            ]):
                in_responsibilities = True
                continue

            if in_responsibilities and any(kw in lower for kw in [
                "qualificat", "requirement", "what you'll need",
                "what we're looking", "skills", "experience",
                "who you are", "nice to have", "preferred",
                "benefits", "compensation", "about us",
            ]):
                in_responsibilities = False
                continue

            if stripped and (
                stripped.startswith("•") or stripped.startswith("-") or
                stripped.startswith("–") or re.match(r"^\d+[\.\)]\s", stripped)
            ):
                bullet_text = re.sub(r"^[•\-–\d\.\)]+\s*", "", stripped)
                if len(bullet_text) > 10:
                    keywords = self._extract_keywords_from_text(bullet_text)
                    analysis.responsibilities.append(
                        Responsibility(text=bullet_text, keywords=keywords)
                    )

    def _extract_keywords_from_text(self, text: str) -> list[str]:
        text_lower = _normalize(text)
        keywords = []
        for d in [PROGRAMMING_LANGUAGES, FRAMEWORKS, DATABASES, INFRASTRUCTURE, TOOLS]:
            keywords.extend(_find_matches(text_lower, d))
        return keywords

    def _analyze_term_frequency(self, text: str, analysis: JobAnalysis):
        """Extract frequently repeated terms, but only technical ones.

        Filters out common English words, JD boilerplate, and generic
        terms that aren't real ATS keywords.
        """
        words = re.findall(r"\b[a-zA-Z][a-zA-Z+#.]{1,30}\b", text)
        word_counts = Counter(w.lower() for w in words)

        stop_words = {
            # Common English
            "the", "and", "for", "with", "that", "this", "you", "will",
            "are", "our", "have", "from", "your", "can", "all", "about",
            "work", "team", "role", "experience", "ability", "strong",
            "including", "such", "well", "also", "may", "new", "join",
            "help", "across", "into", "other", "using", "use", "used",
            "provide", "support", "ensure", "develop", "create", "build",
            "working", "looking", "seeking", "ideal", "candidate",
            "responsible", "required", "preferred", "minimum", "years",
            "etc", "e.g.", "i.e.",
            # JD boilerplate
            "skills", "knowledge", "understanding", "familiarity",
            "expertise", "proficiency", "technologies", "concepts",
            "modern", "clean", "scalable", "reliable", "robust",
            "production", "complex", "based", "driven",
            "full", "stack", "end", "cross", "self",
            "must", "need", "like", "plus", "bonus",
            "company", "product", "products", "services", "service",
            "management", "building", "engineer", "engineering",
            "development", "quality", "high", "best", "first",
        }

        for word, count in word_counts.most_common(50):
            if count >= 2 and word not in stop_words and len(word) > 3:
                already = any(word == i.name.lower() for i in analysis.all_skills_flat())
                if not already:
                    analysis.repeated_terms.append(
                        WeightedItem(name=word, importance=min(1.0, count / 5.0), category="repeated_term")
                    )

    def _extract_soft_skills(self, text_lower: str, analysis: JobAnalysis):
        for pattern in SOFT_SKILLS_PATTERNS:
            if re.search(pattern, text_lower):
                name = pattern.replace(".", " ").replace("\\", "")
                analysis.soft_skills.append(
                    WeightedItem(name=name, importance=0.3, category="soft_skill")
                )

    def _assign_weights(self, text: str, analysis: JobAnalysis):
        text_lower = _normalize(text)

        for item_list in [
            analysis.programming_languages, analysis.frameworks,
            analysis.databases, analysis.infrastructure,
            analysis.tools, analysis.methodologies,
        ]:
            for item in item_list:
                count = len(re.findall(re.escape(item.name.lower()), text_lower))
                item.importance = min(1.0, 0.3 + count * 0.15)

        self._classify_required_preferred(text, analysis)

        for resp in analysis.responsibilities:
            resp.importance = min(1.0, 0.4 + len(resp.keywords) * 0.15)

    def _classify_required_preferred(self, text: str, analysis: JobAnalysis):
        lines = text.split("\n")
        in_required = False
        in_preferred = False
        required_keywords: set[str] = set()
        preferred_keywords: set[str] = set()

        for line in lines:
            lower = line.strip().lower()
            if any(kw in lower for kw in ["required", "must have", "qualificat", "minimum", "what you'll need"]):
                in_required = True
                in_preferred = False
                continue
            if any(kw in lower for kw in ["preferred", "nice to have", "bonus", "plus", "desired"]):
                in_required = False
                in_preferred = True
                continue
            if any(kw in lower for kw in ["responsibilit", "about us", "benefits", "compensation"]):
                in_required = False
                in_preferred = False
                continue

            if in_required:
                for kw in self._extract_keywords_from_text(line):
                    required_keywords.add(kw.lower())
            elif in_preferred:
                for kw in self._extract_keywords_from_text(line):
                    preferred_keywords.add(kw.lower())

        # Track what's already in required/preferred to avoid duplicates
        required_seen = {s.name.lower() for s in analysis.required_skills}
        preferred_seen = {s.name.lower() for s in analysis.preferred_skills}

        for item_list in [
            analysis.programming_languages, analysis.frameworks,
            analysis.databases, analysis.infrastructure, analysis.tools,
        ]:
            for item in item_list:
                name_lower = item.name.lower()
                if name_lower in required_keywords:
                    item.importance = min(1.0, item.importance + 0.3)
                    if name_lower not in required_seen:
                        analysis.required_skills.append(
                            WeightedItem(name=item.name, importance=item.importance, category=item.category)
                        )
                        required_seen.add(name_lower)
                elif name_lower in preferred_keywords:
                    item.importance = min(1.0, item.importance + 0.1)
                    if name_lower not in preferred_seen and name_lower not in required_seen:
                        analysis.preferred_skills.append(
                            WeightedItem(name=item.name, importance=item.importance, category=item.category)
                        )
                        preferred_seen.add(name_lower)

    def _build_ats_phrases(self, analysis: JobAnalysis):
        phrases = []
        for src in [analysis.programming_languages, analysis.frameworks,
                     analysis.databases, analysis.infrastructure, analysis.tools]:
            for item in src:
                phrases.append(item.name)
        for resp in analysis.responsibilities:
            for kw in resp.keywords:
                if kw not in phrases:
                    phrases.append(kw)
        analysis.ats_phrases = phrases

    # ----------------------------------------------------------------
    # STAGE 2: LLM Enrichment
    # ----------------------------------------------------------------

    def _llm_enrich(self, jd_text: str, analysis: JobAnalysis):
        """Call Claude to enrich the deterministic analysis."""
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        user_msg = JD_ANALYSIS_USER_TEMPLATE.format(jd_text=jd_text)

        response = client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=JD_ANALYSIS_SYSTEM_V1,
            messages=[{"role": "user", "content": user_msg}],
        )

        response_text = response.content[0].text.strip()

        # Parse JSON — handle markdown code blocks
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]

        llm_data = self._parse_json_robust(response_text)

        # Merge LLM results into deterministic baseline
        self._merge_llm_results(llm_data, analysis)

    @staticmethod
    def _parse_json_robust(text: str) -> dict:
        """Parse JSON with recovery for truncated or malformed responses."""
        text = text.strip()

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try fixing truncated JSON by closing open structures
        # Walk the string to figure out what's still open
        fixed = text
        open_braces = 0
        open_brackets = 0
        in_string = False
        escape_next = False

        for ch in fixed:
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                open_braces += 1
            elif ch == '}':
                open_braces -= 1
            elif ch == '[':
                open_brackets += 1
            elif ch == ']':
                open_brackets -= 1

        # If we're inside a string, close it
        if in_string:
            fixed += '"'

        # Close open arrays and objects
        fixed += ']' * max(0, open_brackets)
        fixed += '}' * max(0, open_braces)

        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # Last resort: try to find the largest valid JSON substring
        # by trimming from the end until we get valid JSON
        for trim in range(1, min(500, len(text))):
            candidate = text[:-trim]
            # Try closing structures
            ob = candidate.count('{') - candidate.count('}')
            ab = candidate.count('[') - candidate.count(']')
            attempt = candidate + ']' * max(0, ab) + '}' * max(0, ob)
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue

        raise ValueError(f"Could not parse LLM response as JSON (length={len(text)})")

    @staticmethod
    def _clean_skill_name(name: str) -> str:
        """Strip generic suffixes from LLM-extracted skill names.

        "LLM technologies" → "LLMs"
        "Database design and management" → "databases"
        "Python expertise" → "Python"
        "CI/CD pipelines" → "CI/CD"
        """
        import re
        # Known mappings
        REMAP = {
            "llm technologies": "LLMs",
            "llm technology": "LLMs",
            "ai/ml concepts": "AI/ML",
            "ai/ml technologies": "AI/ML",
            "database design and management": "databases",
            "database management": "databases",
            "database design": "databases",
            "full-stack development": "full-stack",
            "full stack development": "full-stack",
            "data structures and algorithms": "data structures",
            "ci/cd pipelines": "CI/CD",
            "restful apis": "REST APIs",
            "restful api development": "REST APIs",
        }
        lower = name.lower().strip()
        if lower in REMAP:
            return REMAP[lower]

        # Strip generic suffixes
        SUFFIXES = [
            " technologies", " technology", " concepts", " expertise",
            " experience", " skills", " proficiency", " knowledge",
            " development", " management", " pipelines", " services",
        ]
        for suffix in SUFFIXES:
            if lower.endswith(suffix):
                stripped = name[: len(name) - len(suffix)].strip()
                if len(stripped) > 1:
                    return stripped

        return name

    def _merge_llm_results(self, llm_data: dict, analysis: JobAnalysis):
        """Merge LLM analysis into the deterministic baseline.

        Rules:
        - LLM can ADD skills/responsibilities not found by deterministic
        - LLM can ADJUST importance weights (average with deterministic)
        - LLM can UPGRADE a skill from preferred to required
        - LLM CANNOT remove anything the deterministic stage found
        """
        # Update job title if LLM found a better one
        llm_title = llm_data.get("job_title", "")
        if llm_title and (not analysis.job_title or len(llm_title) > len(analysis.job_title)):
            analysis.job_title = llm_title

        # Update company/location if found
        if llm_data.get("company") and not analysis.company:
            analysis.company = llm_data["company"]
        if llm_data.get("location") and not analysis.location:
            analysis.location = llm_data["location"]

        # Update job type
        if llm_data.get("job_type") and not analysis.job_type:
            analysis.job_type = llm_data["job_type"]

        # Merge required skills
        existing_names = {s.name.lower() for s in analysis.all_skills_flat()}

        for skill_data in llm_data.get("required_skills", []):
            name = self._clean_skill_name(skill_data.get("name", ""))
            importance = skill_data.get("importance", 0.7)
            category = skill_data.get("category", "")

            name_lower = name.lower().strip()
            if not name_lower:
                continue

            # Check if it exists in deterministic results
            existing = self._find_existing_skill(analysis, name_lower)
            if existing:
                # Adjust importance: average of deterministic and LLM
                existing.importance = min(1.0, (existing.importance + importance) / 2 + 0.15)
                # Ensure it's in required_skills
                if not any(s.name.lower() == name_lower for s in analysis.required_skills):
                    analysis.required_skills.append(
                        WeightedItem(name=existing.name, importance=existing.importance, category=existing.category)
                    )
            elif name_lower not in existing_names:
                # New skill from LLM — add it
                item = WeightedItem(name=name, importance=importance, category=category)
                self._add_to_category(analysis, item, category)
                analysis.required_skills.append(item)
                existing_names.add(name_lower)

        # Merge preferred skills
        for skill_data in llm_data.get("preferred_skills", []):
            name = self._clean_skill_name(skill_data.get("name", ""))
            importance = skill_data.get("importance", 0.4)
            category = skill_data.get("category", "")

            name_lower = name.lower().strip()
            if not name_lower:
                continue

            existing = self._find_existing_skill(analysis, name_lower)
            if existing:
                existing.importance = min(1.0, (existing.importance + importance) / 2)
                if not any(s.name.lower() == name_lower for s in analysis.preferred_skills):
                    if not any(s.name.lower() == name_lower for s in analysis.required_skills):
                        analysis.preferred_skills.append(
                            WeightedItem(name=existing.name, importance=existing.importance, category=existing.category)
                        )
            elif name_lower not in existing_names:
                item = WeightedItem(name=name, importance=importance, category=category)
                self._add_to_category(analysis, item, category)
                analysis.preferred_skills.append(item)
                existing_names.add(name_lower)

        # Merge responsibilities
        existing_resp_texts = {r.text.lower()[:50] for r in analysis.responsibilities}
        for resp_data in llm_data.get("responsibilities", []):
            text = resp_data.get("text", "")
            if not text or text.lower()[:50] in existing_resp_texts:
                continue
            analysis.responsibilities.append(Responsibility(
                text=text,
                importance=resp_data.get("importance", 0.5),
                keywords=resp_data.get("keywords", []),
            ))

        # Merge domain knowledge
        existing_domain = {d.name.lower() for d in analysis.domain_knowledge}
        for dk_data in llm_data.get("domain_knowledge", []):
            name = dk_data.get("name", "")
            if name.lower() not in existing_domain:
                analysis.domain_knowledge.append(
                    WeightedItem(name=name, importance=dk_data.get("importance", 0.4), category="domain")
                )

        # Note: ATS phrases and key_themes are no longer extracted
        # by the LLM prompt. The deterministic pass builds ATS phrases
        # from detected skills, which is sufficient.

        # Final deduplication pass on required/preferred skills lists
        self._deduplicate_skill_lists(analysis)

    def _deduplicate_skill_lists(self, analysis: JobAnalysis):
        """Remove duplicate entries from required_skills and preferred_skills lists.

        Ensures no skill appears more than once in each list (case-insensitive).
        """
        seen_required = set()
        seen_preferred = set()

        # Deduplicate required_skills
        unique_required = []
        for item in analysis.required_skills:
            item_lower = item.name.lower()
            if item_lower not in seen_required:
                unique_required.append(item)
                seen_required.add(item_lower)
        analysis.required_skills = unique_required

        # Deduplicate preferred_skills (exclude any that are in required)
        unique_preferred = []
        for item in analysis.preferred_skills:
            item_lower = item.name.lower()
            if item_lower not in seen_preferred and item_lower not in seen_required:
                unique_preferred.append(item)
                seen_preferred.add(item_lower)
        analysis.preferred_skills = unique_preferred

    def _find_existing_skill(self, analysis: JobAnalysis, name_lower: str) -> Optional[WeightedItem]:
        """Find an existing skill by name across all categories."""
        for item_list in [
            analysis.programming_languages, analysis.frameworks,
            analysis.databases, analysis.infrastructure,
            analysis.tools, analysis.methodologies,
        ]:
            for item in item_list:
                if item.name.lower() == name_lower:
                    return item
        return None

    def _add_to_category(self, analysis: JobAnalysis, item: WeightedItem, category: str):
        """Add a skill to the appropriate category list."""
        cat_map = {
            "language": analysis.programming_languages,
            "framework": analysis.frameworks,
            "database": analysis.databases,
            "infrastructure": analysis.infrastructure,
            "tool": analysis.tools,
            "methodology": analysis.methodologies,
        }
        target = cat_map.get(category, analysis.tools)
        target.append(item)

    # ----------------------------------------------------------------
    # STAGE 3: Categorize Requirements for ATS + Conceptual Matching
    # ----------------------------------------------------------------

    def _recategorize_skill_vs_activity(self, analysis: JobAnalysis):
        """Use LLM to intelligently categorize tools as skills or activities.

        SKILLS: Things you learn/know (Python, SQL, AWS, Docker, Agile, AI)
        ACTIVITIES: Things you create/do (dashboards, reports, documentation, testing, APIs)

        Stores activity items in analysis._activity_tools so _build_deliverables can use them.

        NOTE: This method is OPTIONAL and should not break anything if it fails.
        """
        import json
        from anthropic import Anthropic

        # Initialize empty activity tools list
        analysis._activity_tools = []

        # Collect all items to categorize
        all_items = []
        for skill in analysis.tools:
            all_items.append(skill.name)

        # Short circuit if nothing to categorize
        if not all_items:
            return

        # Create concise list for LLM
        items_str = ", ".join(sorted(set(all_items)))

        # Hardcoded categorization as fallback (if LLM fails)
        activity_keywords = {
            "tableau", "power bi", "looker", "qlik", "kibana",
            "excel", "spreadsheet",
            "test", "testing", "qa",
            "dashboard", "report"
        }

        activity_names_fallback = set(
            name for name in all_items
            if any(kw in name.lower() for kw in activity_keywords)
        )

        prompt = f"""Categorize each item as SKILL (something you learn/know) or ACTIVITY (something you create/do).

Items: {items_str}

Respond with ONLY valid JSON:
{{"skill": ["..."], "activity": ["..."]}}"""

        try:
            client = Anthropic()
            response = client.messages.create(
                model="claude-opus-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )

            if not response.content or not response.content[0].text:
                logger.warning("LLM categorization returned empty response, using fallback")
                activity_names = activity_names_fallback
            else:
                text = response.content[0].text.strip()

                # Extract JSON if wrapped in markdown
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]

                categorization = json.loads(text.strip())
                activity_names = set(categorization.get("activity", []))

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse categorization JSON: {str(e)[:50]}, using fallback")
            activity_names = activity_names_fallback
        except Exception as e:
            logger.warning(f"LLM categorization failed: {str(e)[:50]}, using fallback")
            activity_names = activity_names_fallback

        # Recategorize based on results
        analysis._activity_tools = [tool for tool in analysis.tools if tool.name in activity_names]
        analysis.tools = [tool for tool in analysis.tools if tool.name not in activity_names]

    def _categorize_requirements_for_ats(self, analysis: JobAnalysis):
        """Transform existing skills into categorized requirements with ATS + theme tracking.

        Creates JDRequirement objects that track what ATS will find
        vs what humans/LLM understand conceptually.

        This is incremental: adds to analysis without modifying existing fields.
        """
        # First: Recategorize skills vs activities using LLM (if available)
        # This ensures tools like Tableau are treated as activities, not skills
        if self._use_llm:
            try:
                self._recategorize_skill_vs_activity(analysis)
            except Exception as e:
                # If categorization fails, continue with original categorization
                # This should never happen given the inner error handling, but just in case
                logger.error(f"Unexpected error in skill/activity categorization: {str(e)[:100]}")
                analysis._activity_tools = []

        # Technical requirements from extracted technologies
        self._build_technical_requirements(analysis)

        # Concrete deliverables from responsibilities
        self._build_deliverables(analysis)

        # Behavioral requirements (soft skills) noted but not scored
        self._build_behavioral_requirements(analysis)

        # Gates from extracted education/experience
        self._build_gates(analysis)

    def _build_technical_requirements(self, analysis: JobAnalysis):
        """Convert extracted technologies into JDRequirement objects with ATS tracking.

        NOTE: Activity-focused items (Tableau, Power BI, Excel, etc.) have already been
        removed from analysis.tools by _recategorize_skill_vs_activity().
        We only process skills here.
        """
        # Map each source of technologies to JDRequirement
        for item in analysis.programming_languages:
            analysis.technical_requirements.append(JDRequirement(
                keyword_phrase=item.name,
                theme="Programming Language",
                requirement_level="required" if item in analysis.required_skills else "preferred",
                requirement_type="keyword",
                ats_searchable=[item.name.lower()],  # E.g., ["python"]
                theme_indicators=[item.name.lower()],  # Direct match only
                importance=item.importance,
            ))

        for item in analysis.frameworks:
            analysis.technical_requirements.append(JDRequirement(
                keyword_phrase=item.name,
                theme="Framework/Library",
                requirement_level="required" if item in analysis.required_skills else "preferred",
                requirement_type="keyword",
                ats_searchable=[item.name.lower()],
                theme_indicators=[item.name.lower()],
                importance=item.importance,
            ))

        for item in analysis.databases:
            analysis.technical_requirements.append(JDRequirement(
                keyword_phrase=item.name,
                theme="Database",
                requirement_level="required" if item in analysis.required_skills else "preferred",
                requirement_type="keyword",
                ats_searchable=[item.name.lower()],
                theme_indicators=[item.name.lower()],
                importance=item.importance,
            ))

        for item in analysis.infrastructure:
            analysis.technical_requirements.append(JDRequirement(
                keyword_phrase=item.name,
                theme="Infrastructure/Platform",
                requirement_level="required" if item in analysis.required_skills else "preferred",
                requirement_type="keyword",
                ats_searchable=[item.name.lower()],
                theme_indicators=[item.name.lower()],
                importance=item.importance,
            ))

        # Add remaining tools (activities have been removed by _recategorize_skill_vs_activity)
        for item in analysis.tools:
            analysis.technical_requirements.append(JDRequirement(
                keyword_phrase=item.name,
                theme="Tool/Service",
                requirement_level="required" if item in analysis.required_skills else "preferred",
                requirement_type="keyword",
                ats_searchable=[item.name.lower()],
                theme_indicators=[item.name.lower()],
                importance=item.importance,
            ))

        for item in analysis.methodologies:
            # Methodologies are concepts, not keywords
            ats_terms, theme_terms = self._get_methodology_indicators(item.name)
            analysis.technical_requirements.append(JDRequirement(
                keyword_phrase=item.name,
                theme="Development Methodology/Concept",
                requirement_level="required" if item in analysis.required_skills else "preferred",
                requirement_type="concept",
                ats_searchable=ats_terms,  # What ATS might find
                theme_indicators=theme_terms,  # What demonstrates this concept
                importance=item.importance,
            ))

    def _get_methodology_indicators(self, methodology: str) -> tuple[list[str], list[str]]:
        """Get ATS terms and theme indicators for a methodology.

        Examples:
        - "Agile" → ats_searchable=["agile", "scrum"], theme_indicators=["scrum", "sprints", "agile"]
        - "CI/CD" → ats_searchable=["ci/cd", "ci cd", "continuous"], theme_indicators=["deployment", "pipeline", "continuous"]
        """
        methodology_lower = methodology.lower()

        # Mapping of methodologies to their indicators
        indicators_map = {
            "agile": {
                "ats": ["agile", "scrum", "sprint"],
                "theme": ["scrum", "sprint", "iterative", "agile", "sprints"],
            },
            "scrum": {
                "ats": ["scrum", "agile"],
                "theme": ["scrum", "sprint", "agile", "iterative", "standup"],
            },
            "ci/cd": {
                "ats": ["ci/cd", "ci cd", "continuous"],
                "theme": ["continuous integration", "continuous deployment", "ci/cd", "pipeline", "automated", "deployment"],
            },
            "devops": {
                "ats": ["devops", "dev ops"],
                "theme": ["devops", "deployment", "infrastructure", "automation", "continuous"],
            },
            "microservices": {
                "ats": ["microservices", "microservice"],
                "theme": ["microservices", "service", "distributed", "architecture"],
            },
            "machine learning": {
                "ats": ["machine learning", "ml"],
                "theme": ["machine learning", "ml", "models", "training", "algorithms"],
            },
            "deep learning": {
                "ats": ["deep learning"],
                "theme": ["deep learning", "neural", "networks", "training"],
            },
        }

        mapping = indicators_map.get(methodology_lower, {})
        return (
            mapping.get("ats", [methodology_lower]),
            mapping.get("theme", [methodology_lower]),
        )

    def _build_deliverables(self, analysis: JobAnalysis):
        """Extract concrete deliverables from responsibilities and tools.

        Deliverables are things you CREATE or DO, not technologies you know.
        Examples: dashboards, reports, documentation, tests, APIs, monitoring systems
        """
        # Common deliverables to look for in responsibility text
        deliverable_patterns = {
            "dashboard": ["dashboard", "dashboards", "report", "visualization", "charts"],
            "API": ["api", "rest", "endpoint", "graphql"],
            "documentation": ["document", "documentation", "docs", "guide"],
            "testing": ["test", "testing", "qa", "quality", "automated testing"],
            "database": ["database", "schema", "query", "optimization"],
            "deployment": ["deploy", "deployment", "production", "release"],
            "monitoring": ["monitoring", "alerting", "logging", "observability"],
        }

        found_deliverables = set()

        # First, extract from responsibility text using pattern matching
        for resp in analysis.responsibilities:
            resp_lower = resp.text.lower()
            for deliverable, keywords in deliverable_patterns.items():
                if any(kw in resp_lower for kw in keywords) and deliverable not in found_deliverables:
                    analysis.deliverables.append(ConcreteDeliverable(
                        phrase=deliverable,
                        requirement_level="required",
                        ats_searchable=keywords,
                        importance=0.6,
                    ))
                    found_deliverables.add(deliverable)

        # Second, add tools that were categorized as activities by LLM
        # (stored in analysis._activity_tools by _recategorize_skill_vs_activity)
        if hasattr(analysis, "_activity_tools"):
            for tool in analysis._activity_tools:
                tool_name_lower = tool.name.lower()
                phrase = f"{tool.name} deliverables"  # Generic phrase for the tool

                # Try to be more specific based on tool type
                if any(x in tool_name_lower for x in ["tableau", "power bi", "looker", "qlik", "kibana"]):
                    phrase = f"{tool.name} dashboards/reports"
                elif any(x in tool_name_lower for x in ["excel", "spreadsheet"]):
                    phrase = f"{tool.name} reports"
                elif any(x in tool_name_lower for x in ["test", "qa"]):
                    phrase = f"{tool.name} testing"

                if phrase not in found_deliverables:
                    analysis.deliverables.append(ConcreteDeliverable(
                        phrase=phrase,
                        requirement_level="required" if tool in analysis.required_skills else "preferred",
                        ats_searchable=[tool_name_lower],
                        importance=tool.importance,
                    ))
                    found_deliverables.add(phrase)

    def _build_behavioral_requirements(self, analysis: JobAnalysis):
        """Extract behavioral/soft requirements from soft skills and descriptions."""
        # Common soft skills to note (not scored)
        soft_skill_keywords = {
            "communication": ["communication", "communicate", "verbal", "written"],
            "collaboration": ["collaboration", "collaborate", "team", "cross-team"],
            "leadership": ["leadership", "lead", "mentor", "team lead"],
            "problem-solving": ["problem", "solving", "critical thinking"],
            "self-motivation": ["self-motivated", "self-driven", "motivation"],
        }

        for skill_name, keywords in soft_skill_keywords.items():
            for soft_skill in analysis.soft_skills:
                if any(kw in soft_skill.name.lower() for kw in keywords):
                    analysis.behavioral_requirements.append(BehavioralRequirement(
                        phrase=skill_name,
                        requirement_level="required",
                        evidence_indicators=keywords,
                        importance=0.4,
                    ))
                    break

    def _build_gates(self, analysis: JobAnalysis):
        """Extract education, experience, and eligibility requirements."""
        # Simple heuristic: look for keywords in raw JD text
        text_lower = analysis.raw_text.lower()

        # Education gate
        if any(term in text_lower for term in ["bachelor", "bs ", "b.s.", "degree in"]):
            analysis.education_requirements = EducationGate(
                degree_level="Bachelor's",
                required=True,
            )

        if any(term in text_lower for term in ["master", "ms ", "m.s.", "mba"]):
            analysis.education_requirements = EducationGate(
                degree_level="Master's",
                required=False,  # Usually preferred, not required
            )

        # Experience gate
        if "intern" in text_lower:
            analysis.experience_requirements = ExperienceGate(
                minimum_years=0.0,
                experience_level="intern",
                required=True,
            )
        elif "junior" in text_lower:
            analysis.experience_requirements = ExperienceGate(
                minimum_years=0.0,
                experience_level="junior",
                required=True,
            )
        elif any(term in text_lower for term in ["5+ years", "5 years", "5+ yrs"]):
            analysis.experience_requirements = ExperienceGate(
                minimum_years=5.0,
                experience_level="senior",
                required=True,
            )

        # Eligibility gate
        if any(term in text_lower for term in ["us citizen", "u.s. citizen", "us person"]):
            analysis.eligibility_requirements = EligibilityGate(
                work_authorization="US Citizen required",
                required=True,
            )
        elif any(term in text_lower for term in ["security clearance", "secret", "top secret"]):
            if "secret" in text_lower and "top secret" not in text_lower:
                analysis.eligibility_requirements = EligibilityGate(
                    security_clearance="Secret",
                    required=False,
                )
            elif "top secret" in text_lower:
                analysis.eligibility_requirements = EligibilityGate(
                    security_clearance="Top Secret",
                    required=False,
                )
