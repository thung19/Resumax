"use client";

import { useEffect, useState, useCallback } from "react";

interface CopyModeProps {
  resumeId: string;
  apiUrl: string;
  isTailored: boolean;
}

interface ResumeData {
  contact: {
    name: string;
    email?: string;
    phone?: string;
    linkedin?: string;
    github?: string;
  };
  sections: Array<{
    id: string;
    type: string;
    title: string;
    experience_entries: Array<{
      id: string;
      company: string;
      role: string;
      start_date?: string;
      end_date?: string;
      location?: string;
      bullets: Array<{ id: string; text: string }>;
    }>;
    education_entries: Array<{
      id: string;
      institution: string;
      degree?: string;
      location?: string;
      end_date?: string;
      coursework: string[];
      bullets: Array<{ id: string; text: string }>;
    }>;
    project_entries: Array<{
      id: string;
      name: string;
      bullets: Array<{ id: string; text: string }>;
    }>;
    skill_categories: Array<{
      id: string;
      category: string;
      skills: string[];
    }>;
  }>;
}

function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [text]);

  return (
    <button
      onClick={handleCopy}
      className={`px-1.5 py-0.5 text-[10px] rounded transition-colors ${
        copied
          ? "bg-gray-100 text-gray-700"
          : "bg-gray-100 text-gray-500 hover:bg-gray-200"
      }`}
    >
      {copied ? "Copied" : label || "Copy"}
    </button>
  );
}

export function CopyMode({ resumeId, apiUrl, isTailored }: CopyModeProps) {
  const [data, setData] = useState<ResumeData | null>(null);
  const [fullText, setFullText] = useState("");

  useEffect(() => {
    // Without this guard, toggling `isTailored` while the previous fetch
    // for the old value is still in flight lets the stale response land
    // after the fresh one and silently overwrite it -- e.g. switching from
    // tailored to original view right as a slow "tailored" text fetch was
    // still pending would show the tailored text under the "original"
    // toggle. Matches the cancellation pattern already used correctly in
    // Inspector.tsx.
    let cancelled = false;

    async function load() {
      // Load content
      const contentRes = await fetch(`${apiUrl}/inspect/${resumeId}/content`);
      if (!cancelled && contentRes.ok) setData(await contentRes.json());

      // Load full text
      const textUrl = isTailored
        ? `${apiUrl}/export/${resumeId}/tailored/txt`
        : `${apiUrl}/export/${resumeId}/txt`;
      const textRes = await fetch(textUrl);
      if (!cancelled && textRes.ok) setFullText(await textRes.text());
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [resumeId, apiUrl, isTailored]);

  if (!data) return <div className="p-4 text-sm text-gray-400">Loading...</div>;

  return (
    <div className="p-4 space-y-4 text-sm">
      {/* Copy full resume */}
      <div className="flex items-center gap-2">
        <CopyButton text={fullText} label="Copy Full Resume" />
        <span className="text-xs text-gray-400">{fullText.length} chars</span>
      </div>

      {/* Contact */}
      <div className="border-t border-gray-200 pt-2">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xs font-medium text-gray-600">Contact</span>
        </div>
        {data.contact.name && (
          <CopyRow label="Name" value={data.contact.name} />
        )}
        {data.contact.email && (
          <CopyRow label="Email" value={data.contact.email} />
        )}
        {data.contact.phone && (
          <CopyRow label="Phone" value={data.contact.phone} />
        )}
        {data.contact.linkedin && (
          <CopyRow label="LinkedIn" value={data.contact.linkedin} />
        )}
        {data.contact.github && (
          <CopyRow label="GitHub" value={data.contact.github} />
        )}
      </div>

      {/* Sections */}
      {data.sections.map((section) => (
        <div key={section.id} className="border-t border-gray-200 pt-2">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-medium text-gray-700">{section.title}</span>
            <CopyButton
              text={formatSectionText(section)}
              label="Copy Section"
            />
          </div>

          {/* Experience entries */}
          {section.experience_entries.map((entry) => (
            <div key={entry.id} className="ml-2 mb-2">
              <div className="flex items-center gap-1 mb-0.5">
                <span className="text-xs font-medium text-gray-800">{entry.company}</span>
                <CopyButton
                  text={formatExperienceText(entry)}
                  label="Copy"
                />
              </div>
              <div className="text-[10px] text-gray-500 mb-0.5">
                {entry.role}
                {entry.start_date && ` | ${entry.start_date}`}
                {entry.end_date && ` - ${entry.end_date}`}
                {entry.location && ` | ${entry.location}`}
              </div>
              <CopyRow label="Company" value={entry.company} />
              <CopyRow label="Role" value={entry.role} />
              {entry.location && <CopyRow label="Location" value={entry.location} />}
              {entry.start_date && <CopyRow label="Start" value={entry.start_date} />}
              {entry.end_date && <CopyRow label="End" value={entry.end_date} />}
              {entry.bullets.map((b) => (
                <div key={b.id} className="flex items-start gap-1 mt-0.5">
                  <CopyButton text={b.text} label="Copy" />
                  <span className="text-[10px] text-gray-600 flex-1">
                    {b.text.length > 80 ? b.text.slice(0, 80) + "..." : b.text}
                  </span>
                </div>
              ))}
            </div>
          ))}

          {/* Education entries */}
          {section.education_entries.map((entry) => (
            <div key={entry.id} className="ml-2 mb-2">
              <div className="flex items-center gap-1">
                <span className="text-xs font-medium text-gray-800">{entry.institution}</span>
                <CopyButton
                  text={`${entry.institution}\n${entry.degree || ""}\n${entry.location || ""}`}
                  label="Copy"
                />
              </div>
              {entry.degree && <CopyRow label="Degree" value={entry.degree} />}
              {entry.location && <CopyRow label="Location" value={entry.location} />}
              {entry.coursework.length > 0 && (
                <CopyRow label="Coursework" value={entry.coursework.join(", ")} />
              )}
            </div>
          ))}

          {/* Project entries */}
          {section.project_entries.map((entry) => (
            <div key={entry.id} className="ml-2 mb-2">
              <div className="flex items-center gap-1 mb-0.5">
                <span className="text-xs font-medium text-gray-800">{entry.name}</span>
                <CopyButton
                  text={formatProjectText(entry)}
                  label="Copy"
                />
              </div>
              {entry.bullets.map((b) => (
                <div key={b.id} className="flex items-start gap-1 mt-0.5">
                  <CopyButton text={b.text} label="Copy" />
                  <span className="text-[10px] text-gray-600 flex-1">
                    {b.text.length > 80 ? b.text.slice(0, 80) + "..." : b.text}
                  </span>
                </div>
              ))}
            </div>
          ))}

          {/* Skills */}
          {section.skill_categories.map((cat) => (
            <div key={cat.id} className="ml-2 mb-1">
              <CopyRow label={cat.category} value={cat.skills.join(", ")} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-1 py-0.5">
      <CopyButton text={value} />
      <span className="text-[10px] text-gray-400 w-16 shrink-0">{label}</span>
      <span className="text-[10px] text-gray-600 truncate">{value}</span>
    </div>
  );
}

function formatExperienceText(entry: ResumeData["sections"][0]["experience_entries"][0]): string {
  const lines: string[] = [];
  lines.push(entry.company);
  let role = entry.role;
  if (entry.location) role += ` | ${entry.location}`;
  lines.push(role);
  if (entry.start_date && entry.end_date) {
    lines.push(`${entry.start_date} - ${entry.end_date}`);
  }
  for (const b of entry.bullets) {
    lines.push(`- ${b.text}`);
  }
  return lines.join("\n");
}

function formatProjectText(entry: ResumeData["sections"][0]["project_entries"][0]): string {
  const lines: string[] = [entry.name];
  for (const b of entry.bullets) {
    lines.push(`- ${b.text}`);
  }
  return lines.join("\n");
}

function formatSectionText(section: ResumeData["sections"][0]): string {
  const lines: string[] = [];
  if (section.title) lines.push(section.title);
  for (const e of section.experience_entries) {
    lines.push(formatExperienceText(e));
    lines.push("");
  }
  for (const e of section.education_entries) {
    lines.push(e.institution);
    if (e.degree) lines.push(e.degree);
    if (e.coursework.length > 0) lines.push(`Coursework: ${e.coursework.join(", ")}`);
  }
  for (const e of section.project_entries) {
    lines.push(formatProjectText(e));
    lines.push("");
  }
  for (const c of section.skill_categories) {
    lines.push(`${c.category}: ${c.skills.join(", ")}`);
  }
  return lines.join("\n").trim();
}
