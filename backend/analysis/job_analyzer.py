"""Job Description Analyzer.

Two-stage analysis:
1. Deterministic extraction: keyword matching, term frequency, pattern detection
2. LLM-assisted extraction: Claude for semantic understanding of responsibilities,
   skill categorization, and importance weighting

The deterministic stage runs first and always. The LLM stage enriches.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from backend.models.job_description import (
    JobAnalysis,
    Responsibility,
    WeightedItem,
)


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
    """Normalize text for matching."""
    return text.lower().strip()


def _find_matches(text_lower: str, dictionary: set[str]) -> list[str]:
    """Find all dictionary terms present in text."""
    matches = []
    for term in sorted(dictionary, key=len, reverse=True):
        # Use word boundary matching for short terms
        if len(term) <= 2:
            pattern = rf"\b{re.escape(term)}\b"
        else:
            pattern = re.escape(term)
        if re.search(pattern, text_lower, re.IGNORECASE):
            matches.append(term)
    return matches


class JobAnalyzer:
    """Analyze a job description deterministically."""

    def analyze(self, jd_text: str) -> JobAnalysis:
        """Run the full analysis pipeline."""
        analysis = JobAnalysis(raw_text=jd_text)
        text_lower = _normalize(jd_text)

        # Extract role info
        self._extract_role_info(jd_text, analysis)

        # Extract technologies
        self._extract_technologies(text_lower, analysis)

        # Extract responsibilities
        self._extract_responsibilities(jd_text, analysis)

        # Term frequency analysis
        self._analyze_term_frequency(jd_text, analysis)

        # Extract soft skills
        self._extract_soft_skills(text_lower, analysis)

        # Build ATS phrases
        self._build_ats_phrases(analysis)

        # Assign importance weights based on frequency and position
        self._assign_weights(jd_text, analysis)

        return analysis

    def _extract_role_info(self, text: str, analysis: JobAnalysis):
        """Extract job title, company, location from the JD."""
        lines = text.strip().split("\n")
        # Job title is often the first non-empty line
        for line in lines[:5]:
            line = line.strip()
            if not line:
                continue
            # Skip lines that look like company names or locations
            if any(kw in line.lower() for kw in ["about us", "about the", "company", "overview"]):
                continue
            if not analysis.job_title and len(line) < 100:
                analysis.job_title = line
                break

        # Detect internship / full-time
        if re.search(r"\bintern\b", text, re.I):
            analysis.job_type = "internship"
        elif re.search(r"\bfull.time\b", text, re.I):
            analysis.job_type = "full-time"
        elif re.search(r"\bcontract\b", text, re.I):
            analysis.job_type = "contract"

    def _extract_technologies(self, text_lower: str, analysis: JobAnalysis):
        """Extract and categorize technologies."""
        for lang in _find_matches(text_lower, PROGRAMMING_LANGUAGES):
            analysis.programming_languages.append(
                WeightedItem(name=lang, category="language")
            )

        for fw in _find_matches(text_lower, FRAMEWORKS):
            analysis.frameworks.append(
                WeightedItem(name=fw, category="framework")
            )

        for db in _find_matches(text_lower, DATABASES):
            analysis.databases.append(
                WeightedItem(name=db, category="database")
            )

        for infra in _find_matches(text_lower, INFRASTRUCTURE):
            analysis.infrastructure.append(
                WeightedItem(name=infra, category="infrastructure")
            )

        for tool in _find_matches(text_lower, TOOLS):
            analysis.tools.append(
                WeightedItem(name=tool, category="tool")
            )

        for meth in _find_matches(text_lower, METHODOLOGIES):
            analysis.methodologies.append(
                WeightedItem(name=meth, category="methodology")
            )

    def _extract_responsibilities(self, text: str, analysis: JobAnalysis):
        """Extract responsibility bullets from the JD."""
        lines = text.split("\n")
        in_responsibilities = False

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()

            # Detect responsibility section headers
            if any(kw in lower for kw in [
                "responsibilit", "what you'll do", "what you will do",
                "duties", "role description", "day-to-day",
                "in this role", "you will", "your role",
            ]):
                in_responsibilities = True
                continue

            # Detect end of responsibilities section
            if in_responsibilities and any(kw in lower for kw in [
                "qualificat", "requirement", "what you'll need",
                "what we're looking", "skills", "experience",
                "who you are", "nice to have", "preferred",
                "benefits", "compensation", "about us",
            ]):
                in_responsibilities = False
                continue

            # Extract bullet items
            if stripped and (
                stripped.startswith("•") or
                stripped.startswith("-") or
                stripped.startswith("–") or
                re.match(r"^\d+[\.\)]\s", stripped)
            ):
                bullet_text = re.sub(r"^[•\-–\d\.\)]+\s*", "", stripped)
                if len(bullet_text) > 10:
                    # Extract keywords from this responsibility
                    keywords = self._extract_keywords_from_text(bullet_text)
                    analysis.responsibilities.append(
                        Responsibility(
                            text=bullet_text,
                            keywords=keywords,
                        )
                    )

    def _extract_keywords_from_text(self, text: str) -> list[str]:
        """Extract technology keywords from a text snippet."""
        text_lower = _normalize(text)
        keywords = []
        for dictionary in [PROGRAMMING_LANGUAGES, FRAMEWORKS, DATABASES, INFRASTRUCTURE, TOOLS]:
            keywords.extend(_find_matches(text_lower, dictionary))
        return keywords

    def _analyze_term_frequency(self, text: str, analysis: JobAnalysis):
        """Analyze term frequency to find repeated important terms."""
        # Tokenize and count
        words = re.findall(r"\b[a-zA-Z][a-zA-Z+#.]{1,30}\b", text)
        word_counts = Counter(w.lower() for w in words)

        # Filter stop words and common terms
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
                # Check if it's already captured as a technology
                already_captured = any(
                    word == item.name.lower()
                    for item in analysis.all_skills_flat()
                )
                if not already_captured:
                    analysis.repeated_terms.append(
                        WeightedItem(
                            name=word,
                            importance=min(1.0, count / 5.0),
                            category="repeated_term",
                        )
                    )

    def _extract_soft_skills(self, text_lower: str, analysis: JobAnalysis):
        """Extract soft skills."""
        for pattern in SOFT_SKILLS_PATTERNS:
            if re.search(pattern, text_lower):
                # Clean up the pattern for display
                name = pattern.replace(".", " ").replace("\\", "")
                analysis.soft_skills.append(
                    WeightedItem(name=name, importance=0.3, category="soft_skill")
                )

    def _build_ats_phrases(self, analysis: JobAnalysis):
        """Build common ATS-friendly phrases from the analysis."""
        phrases = []
        for lang in analysis.programming_languages:
            phrases.append(lang.name)
        for fw in analysis.frameworks:
            phrases.append(fw.name)
        for db in analysis.databases:
            phrases.append(db.name)
        for infra in analysis.infrastructure:
            phrases.append(infra.name)
        for tool in analysis.tools:
            phrases.append(tool.name)

        # Add compound phrases from responsibilities
        for resp in analysis.responsibilities:
            for kw in resp.keywords:
                if kw not in phrases:
                    phrases.append(kw)

        analysis.ats_phrases = phrases

    def _assign_weights(self, text: str, analysis: JobAnalysis):
        """Assign importance weights based on frequency and section placement."""
        text_lower = _normalize(text)

        # Count occurrences for weighting
        for item_list in [
            analysis.programming_languages,
            analysis.frameworks,
            analysis.databases,
            analysis.infrastructure,
            analysis.tools,
            analysis.methodologies,
        ]:
            for item in item_list:
                count = len(re.findall(re.escape(item.name.lower()), text_lower))
                # Base weight from frequency
                item.importance = min(1.0, 0.3 + count * 0.15)

        # Required skills get higher weight
        self._classify_required_preferred(text, analysis)

        # Responsibilities get weight from keyword density
        for resp in analysis.responsibilities:
            resp.importance = min(1.0, 0.4 + len(resp.keywords) * 0.15)

    def _classify_required_preferred(self, text: str, analysis: JobAnalysis):
        """Classify skills as required vs preferred based on section placement."""
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

        # Update weights: required skills get boosted, preferred get moderate weight
        for item_list in [
            analysis.programming_languages,
            analysis.frameworks,
            analysis.databases,
            analysis.infrastructure,
            analysis.tools,
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
