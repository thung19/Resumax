"use client";

import { useState, useCallback } from "react";

interface TailorOptions {
  jdText: string;
  useLlm: boolean;
  enforceOnePage: boolean;
  oneLineBullets: boolean;
  enforceSingleLine: boolean;
  maxBulletChars: number;
}

interface JobDescriptionInputProps {
  onTailor: (opts: TailorOptions) => void;
  loading: boolean;
  apiUrl: string;
  resumeId: string;
}

interface JDAnalysis {
  job_title: string;
  company: string;
  location: string;
  job_type: string;
  analysis_method: string;
  llm_error: string | null;
  programming_languages: Array<{ name: string; importance: number }>;
  frameworks: Array<{ name: string; importance: number }>;
  databases: Array<{ name: string; importance: number }>;
  infrastructure: Array<{ name: string; importance: number }>;
  tools: Array<{ name: string; importance: number }>;
  methodologies: Array<{ name: string; importance: number }>;
  domain_knowledge: Array<{ name: string; importance: number }>;
  required_skills: Array<{ name: string; importance: number }>;
  preferred_skills: Array<{ name: string; importance: number }>;
  responsibilities: Array<{ text: string; importance: number }>;
  all_keywords: string[];
}

export function JobDescriptionInput({
  onTailor,
  loading,
  apiUrl,
  resumeId,
}: JobDescriptionInputProps) {
  const [jdText, setJdText] = useState("");
  const [useLlm, setUseLlm] = useState(true);
  const [enforceOnePage, setEnforceOnePage] = useState(true);
  const [oneLineBullets, setOneLineBullets] = useState(true);
  // No UI control toggles these yet — kept as fixed values (matching
  // today's actual behavior) rather than dead state nothing ever sets.
  // enforceSingleLine=false lets multi-line bullets through; maxBulletChars=0
  // tells the backend to auto-detect the per-line character budget.
  const enforceSingleLine = false;
  const maxBulletChars = 0;
  const [analysis, setAnalysis] = useState<JDAnalysis | null>(null);
  const [analyzing, setAnalyzing] = useState(false);

  const handleAnalyze = useCallback(async () => {
    if (!jdText.trim()) return;
    setAnalyzing(true);
    try {
      const res = await fetch(`${apiUrl}/analyze-jd`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jd_text: jdText, resume_id: resumeId }),
      });
      if (res.ok) {
        setAnalysis(await res.json());
      }
    } finally {
      setAnalyzing(false);
    }
  }, [jdText, apiUrl, resumeId]);

  return (
    <div className="p-4 space-y-4">
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Paste Job Description
        </label>
        <textarea
          value={jdText}
          onChange={(e) => setJdText(e.target.value)}
          placeholder="Paste the full job description here..."
          className="w-full h-48 text-sm border border-gray-300 rounded-lg p-3 resize-y focus:ring-2 focus:ring-gray-500 focus:border-gray-500"
        />
      </div>

      <div className="space-y-3">
        <div className="flex items-center gap-3">
          <button
            onClick={handleAnalyze}
            disabled={analyzing || !jdText.trim()}
            className="px-3 py-1.5 text-sm bg-gray-100 text-gray-700 rounded border border-gray-300 hover:bg-gray-200 disabled:opacity-50 flex items-center gap-1.5"
          >
            {analyzing && <Spinner />}
            {analyzing ? "Analyzing JD..." : "Analyze JD"}
          </button>

          <button
            onClick={() => onTailor({ jdText, useLlm, enforceOnePage, oneLineBullets, enforceSingleLine, maxBulletChars })}
            disabled={loading || !jdText.trim()}
            className="px-4 py-1.5 text-sm font-medium text-gray-700 bg-gray-100 border border-gray-300 rounded hover:bg-gray-200 disabled:opacity-50 flex items-center gap-1.5"
          >
            {loading && <Spinner light />}
            {loading ? "Tailoring Resume..." : "Tailor Resume"}
          </button>

          <label className="flex items-center gap-1.5 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={useLlm}
              onChange={(e) => setUseLlm(e.target.checked)}
              className="rounded"
            />
            Use AI
          </label>
        </div>

        {/* Constraints */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-1 border-t border-gray-100">
          <label className="flex items-center gap-1.5 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={enforceOnePage}
              onChange={(e) => setEnforceOnePage(e.target.checked)}
              className="rounded"
            />
            Fit to 1 page
          </label>
          <label className="flex items-center gap-1.5 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={oneLineBullets}
              onChange={(e) => setOneLineBullets(e.target.checked)}
              className="rounded"
            />
            1-line bullets
          </label>
        </div>

      </div>

      {/* JD Analysis display */}
      {analysis && (
        <div className="border-t border-gray-200 pt-3 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-900">
              {analysis.job_title}
              {analysis.company && <span className="font-normal text-gray-500"> at {analysis.company}</span>}
            </h3>
            <div className="flex items-center gap-2 mt-0.5">
              {analysis.location && <span className="text-xs text-gray-400">{analysis.location}</span>}
              {analysis.job_type && <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">{analysis.job_type}</span>}
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${analysis.analysis_method.includes("LLM") ? "bg-gray-50 text-gray-700" : "bg-gray-50 text-gray-500"}`}>
                {analysis.analysis_method}
              </span>
            </div>
            {analysis.llm_error && (
              <div className="text-[10px] text-gray-500 mt-1">LLM fallback: {analysis.llm_error}</div>
            )}
          </div>

          {analysis.required_skills.length > 0 && (
            <SkillGroup label="Required" items={analysis.required_skills} />
          )}
          {analysis.preferred_skills.length > 0 && (
            <SkillGroup label="Preferred" items={analysis.preferred_skills} />
          )}
          {analysis.programming_languages.length > 0 && (
            <SkillGroup label="Languages" items={analysis.programming_languages} />
          )}
          {analysis.frameworks.length > 0 && (
            <SkillGroup label="Frameworks" items={analysis.frameworks} />
          )}
          {analysis.databases.length > 0 && (
            <SkillGroup label="Databases" items={analysis.databases} />
          )}
          {analysis.infrastructure.length > 0 && (
            <SkillGroup label="Infrastructure" items={analysis.infrastructure} />
          )}
          {analysis.tools.length > 0 && (
            <SkillGroup label="Tools" items={analysis.tools} />
          )}
          {analysis.methodologies.length > 0 && (
            <SkillGroup label="Methodologies" items={analysis.methodologies} />
          )}
          {analysis.domain_knowledge.length > 0 && (
            <SkillGroup label="Domain Knowledge" items={analysis.domain_knowledge} />
          )}

          {analysis.responsibilities.length > 0 && (
            <div>
              <div className="text-xs font-medium text-gray-600 mb-1">
                Responsibilities ({analysis.responsibilities.length})
              </div>
              <div className="space-y-1">
                {analysis.responsibilities.slice(0, 6).map((r, i) => (
                  <div key={i} className="text-xs text-gray-500 pl-2 border-l-2 border-gray-200">
                    <span className="text-gray-300 mr-1">{Math.round(r.importance * 100)}%</span>
                    {r.text.length > 100 ? r.text.slice(0, 100) + "..." : r.text}
                  </div>
                ))}
              </div>
            </div>
          )}

          {analysis.all_keywords.length > 0 && (
            <div>
              <div className="text-xs font-medium text-gray-600 mb-1">
                ATS Keywords ({analysis.all_keywords.length})
              </div>
              <div className="flex flex-wrap gap-1">
                {analysis.all_keywords.slice(0, 25).map((kw, i) => (
                  <span
                    key={i}
                    className="px-1.5 py-0.5 text-xs bg-muted-blue text-muted-blue rounded"
                  >
                    {kw}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SkillGroup({
  label,
  items,
}: {
  label: string;
  items: Array<{ name: string; importance: number }>;
}) {
  return (
    <div>
      <div className="text-xs font-medium text-gray-600 mb-1">{label}</div>
      <div className="flex flex-wrap gap-1">
        {items.map((item, i) => (
          <span
            key={i}
            className="px-1.5 py-0.5 text-xs rounded"
            style={{
              backgroundColor: `rgba(107, 127, 160, ${item.importance * 0.2 + 0.05})`,
              color: item.importance > 0.6 ? "#374151" : "#6b7280",
            }}
          >
            {item.name}
          </span>
        ))}
      </div>
    </div>
  );
}

function Spinner({ light }: { light?: boolean }) {
  return (
    <div
      className={`w-3.5 h-3.5 border-2 rounded-full animate-spin ${
        light
          ? "border-white/30 border-t-white"
          : "border-gray-300 border-t-gray-600"
      }`}
    />
  );
}
