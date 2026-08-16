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
)

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


LLM_JD_ANALYSIS_PROMPT = """Analyze this job description and extract structured information.

JOB DESCRIPTION:
{jd_text}

Return a JSON object with these fields:

{{
  "job_title": "exact title from the posting",
  "company": "company name if mentioned",
  "location": "location if mentioned",
  "job_type": "internship | full-time | part-time | contract",
  "seniority": "intern | junior | mid | senior | staff | lead | principal",

  "required_skills": [
    {{"name": "skill name", "importance": 0.0-1.0, "category": "language|framework|database|infrastructure|tool|methodology|domain|soft_skill"}}
  ],
  "preferred_skills": [
    {{"name": "skill name", "importance": 0.0-1.0, "category": "..."}}
  ],

  "responsibilities": [
    {{"text": "responsibility description", "importance": 0.0-1.0, "keywords": ["relevant", "technical", "terms"]}}
  ],

  "domain_knowledge": [
    {{"name": "domain area", "importance": 0.0-1.0}}
  ],

  "key_themes": ["recurring themes or priorities in the role"],

  "ats_phrases": ["exact phrases a resume scanner would match on"]
}}

RULES:
- importance: 1.0 = explicitly required/emphasized, 0.7 = strongly implied, 0.4 = nice-to-have, 0.2 = barely mentioned
- For skills: capture named technologies, specific tools, frameworks, languages, databases, cloud platforms, and methodologies. Do NOT extract generic activity descriptions (debugging, testing, building), soft skills (communication, collaboration, problem-solving, leadership, teamwork), or common verbs as skills. If it wouldn't appear as a listed skill on a resume's Skills section, don't include it.
- For responsibilities: extract the actual duties, not just section headers. Include responsibilities from narrative paragraphs, not only bullet points
- For required vs preferred: classify based on the SECTION they appear in and the language used ("must have" vs "nice to have" vs "bonus")
- For domain_knowledge: capture industry-specific knowledge (e.g., "financial services", "healthcare", "e-commerce")
- For ats_phrases: include the exact compound phrases an ATS would scan for (e.g., "full-stack development", "RESTful APIs", "CI/CD pipelines"). Do NOT include soft skills or generic verbs as ATS phrases.
- For key_themes: what does this role REALLY care about? (e.g., "shipping quickly", "code quality", "cross-team collaboration")

Return ONLY valid JSON, no markdown code blocks."""


def _normalize(text: str) -> str:
    return text.lower().strip()


def _find_matches(text_lower: str, dictionary: set[str]) -> list[str]:
    matches = []
    for term in sorted(dictionary, key=len, reverse=True):
        if len(term) <= 2:
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
                self._llm_error = str(e)
                logger.warning(f"LLM JD analysis failed, using deterministic only: {e}")

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
        for lang in _find_matches(text_lower, PROGRAMMING_LANGUAGES):
            analysis.programming_languages.append(WeightedItem(name=lang, category="language"))
        for fw in _find_matches(text_lower, FRAMEWORKS):
            analysis.frameworks.append(WeightedItem(name=fw, category="framework"))
        for db in _find_matches(text_lower, DATABASES):
            analysis.databases.append(WeightedItem(name=db, category="database"))
        for infra in _find_matches(text_lower, INFRASTRUCTURE):
            analysis.infrastructure.append(WeightedItem(name=infra, category="infrastructure"))
        for tool in _find_matches(text_lower, TOOLS):
            analysis.tools.append(WeightedItem(name=tool, category="tool"))
        for meth in _find_matches(text_lower, METHODOLOGIES):
            analysis.methodologies.append(WeightedItem(name=meth, category="methodology"))

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
        words = re.findall(r"\b[a-zA-Z][a-zA-Z+#.]{1,30}\b", text)
        word_counts = Counter(w.lower() for w in words)

        stop_words = {
            "the", "and", "for", "with", "that", "this", "you", "will",
            "are", "our", "have", "from", "your", "can", "all", "about",
            "work", "team", "role", "experience", "ability", "strong",
            "including", "such", "well", "also", "may", "new", "join",
            "help", "across", "into", "other", "using", "use", "used",
            "provide", "support", "ensure", "develop", "create", "build",
            "working", "looking", "seeking", "ideal", "candidate",
            "responsible", "required", "preferred", "minimum", "years",
            "etc", "e.g.", "i.e.",
        }

        for word, count in word_counts.most_common(50):
            if count >= 2 and word not in stop_words and len(word) > 2:
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

        for item_list in [
            analysis.programming_languages, analysis.frameworks,
            analysis.databases, analysis.infrastructure, analysis.tools,
        ]:
            for item in item_list:
                name_lower = item.name.lower()
                if name_lower in required_keywords:
                    item.importance = min(1.0, item.importance + 0.3)
                    analysis.required_skills.append(
                        WeightedItem(name=item.name, importance=item.importance, category=item.category)
                    )
                elif name_lower in preferred_keywords:
                    item.importance = min(1.0, item.importance + 0.1)
                    analysis.preferred_skills.append(
                        WeightedItem(name=item.name, importance=item.importance, category=item.category)
                    )

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

        prompt = LLM_JD_ANALYSIS_PROMPT.format(jd_text=jd_text)

        response = client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
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
            name = skill_data.get("name", "")
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
            name = skill_data.get("name", "")
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

        # Merge ATS phrases
        existing_ats = {p.lower() for p in analysis.ats_phrases}
        for phrase in llm_data.get("ats_phrases", []):
            if phrase.lower() not in existing_ats:
                analysis.ats_phrases.append(phrase)
                existing_ats.add(phrase.lower())

        # Add key themes as repeated terms
        for theme in llm_data.get("key_themes", []):
            theme_lower = theme.lower()
            if not any(t.name.lower() == theme_lower for t in analysis.repeated_terms):
                analysis.repeated_terms.append(
                    WeightedItem(name=theme, importance=0.6, category="theme")
                )

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
