"use client";

import { useEffect, useState } from "react";

interface InspectorProps {
  resumeId: string;
  tab: "content" | "layout" | "formatting";
  apiUrl: string;
}

export function Inspector({ resumeId, tab, apiUrl }: InspectorProps) {
  const [data, setData] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);

      try {
        const endpoint =
          tab === "formatting"
            ? `${apiUrl}/inspect/${resumeId}/formatting`
            : `${apiUrl}/inspect/${resumeId}/${tab}`;

        const res = await fetch(endpoint);
        if (!res.ok) throw new Error("Failed to load");
        const json = await res.json();
        if (!cancelled) setData(json);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Error");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [resumeId, tab, apiUrl]);

  if (loading) {
    return (
      <div className="p-4 text-sm text-gray-400">Loading...</div>
    );
  }

  if (error) {
    return (
      <div className="p-4 text-sm text-gray-500">{error}</div>
    );
  }

  if (!data) return null;

  if (tab === "content") {
    return <ContentView data={data} />;
  }

  if (tab === "layout") {
    return <LayoutView data={data} />;
  }

  // Formatting tab — show raw JSON
  return (
    <pre className="p-4 text-xs font-mono text-gray-700 whitespace-pre-wrap overflow-auto">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

function ContentView({ data }: { data: unknown }) {
  const content = data as {
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
  };

  return (
    <div className="p-4 text-sm space-y-4">
      {/* Contact */}
      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Contact</h3>
        <div className="text-gray-600 space-y-0.5">
          <div className="font-medium">{content.contact.name}</div>
          {content.contact.email && <div>{content.contact.email}</div>}
          {content.contact.phone && <div>{content.contact.phone}</div>}
          {content.contact.linkedin && <div>{content.contact.linkedin}</div>}
          {content.contact.github && <div>{content.contact.github}</div>}
        </div>
      </div>

      {/* Sections */}
      {content.sections.map((section) => (
        <div key={section.id}>
          <h3 className="font-semibold text-gray-900 mb-1 flex items-center gap-2">
            {section.title || "Untitled"}
            <span className="text-xs font-normal text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">
              {section.type}
            </span>
          </h3>

          {/* Experience */}
          {section.experience_entries.map((entry) => (
            <div key={entry.id} className="ml-2 mb-2 border-l-2 border-gray-200 pl-3">
              <div className="font-medium text-gray-800">{entry.company}</div>
              <div className="text-gray-500 text-xs">
                {entry.role} {entry.start_date && `| ${entry.start_date}`}
                {entry.end_date && ` – ${entry.end_date}`}
                {entry.location && ` | ${entry.location}`}
              </div>
              {entry.bullets.map((b) => (
                <div key={b.id} className="text-gray-600 text-xs ml-2 mt-0.5">
                  <span className="text-gray-300 mr-1">{b.id}</span>
                  {b.text.length > 100 ? b.text.slice(0, 100) + "..." : b.text}
                </div>
              ))}
            </div>
          ))}

          {/* Education */}
          {section.education_entries.map((entry) => (
            <div key={entry.id} className="ml-2 mb-2 border-l-2 border-gray-200 pl-3">
              <div className="font-medium text-gray-800">{entry.institution}</div>
              {entry.degree && <div className="text-gray-500 text-xs">{entry.degree}</div>}
              {entry.coursework.length > 0 && (
                <div className="text-gray-400 text-xs mt-0.5">
                  Coursework: {entry.coursework.join(", ")}
                </div>
              )}
            </div>
          ))}

          {/* Projects */}
          {section.project_entries.map((entry) => (
            <div key={entry.id} className="ml-2 mb-2 border-l-2 border-gray-200 pl-3">
              <div className="font-medium text-gray-800">{entry.name}</div>
              {entry.bullets.map((b) => (
                <div key={b.id} className="text-gray-600 text-xs ml-2 mt-0.5">
                  <span className="text-gray-300 mr-1">{b.id}</span>
                  {b.text.length > 100 ? b.text.slice(0, 100) + "..." : b.text}
                </div>
              ))}
            </div>
          ))}

          {/* Skills */}
          {section.skill_categories.map((cat) => (
            <div key={cat.id} className="ml-2 text-xs text-gray-600 mb-1">
              <span className="font-medium">{cat.category}:</span>{" "}
              {cat.skills.join(", ")}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function LayoutView({ data }: { data: unknown }) {
  const layout = data as {
    page: {
      width_in: number;
      height_in: number;
      margin_top_in: number;
      margin_bottom_in: number;
      margin_left_in: number;
      margin_right_in: number;
    };
    default_font: { family: string; size_pt: number };
    styles: Record<
      string,
      {
        font: { family: string; size_pt: number; bold: boolean; italic: boolean };
        spacing: { space_before_pt: number; space_after_pt: number; line_spacing: number | null };
        alignment: string;
        layout_mode: string;
      }
    >;
    horizontal_rules: Record<string, { source_type: string; width_pt: number }>;
  };

  return (
    <div className="p-4 text-sm space-y-4">
      {/* Page */}
      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Page</h3>
        <div className="text-xs text-gray-600 font-mono space-y-0.5">
          <div>Size: {layout.page.width_in}" x {layout.page.height_in}"</div>
          <div>
            Margins: T={layout.page.margin_top_in}" B={layout.page.margin_bottom_in}"
            L={layout.page.margin_left_in}" R={layout.page.margin_right_in}"
          </div>
        </div>
      </div>

      {/* Default font */}
      <div>
        <h3 className="font-semibold text-gray-900 mb-1">Default Font</h3>
        <div className="text-xs text-gray-600 font-mono">
          {layout.default_font.family} {layout.default_font.size_pt}pt
        </div>
      </div>

      {/* Styles */}
      <div>
        <h3 className="font-semibold text-gray-900 mb-1">
          Styles ({Object.keys(layout.styles).length})
        </h3>
        <div className="space-y-2">
          {Object.entries(layout.styles).map(([name, style]) => (
            <div key={name} className="bg-gray-50 rounded p-2">
              <div className="font-medium text-gray-800 text-xs">{name}</div>
              <div className="text-xs text-gray-500 font-mono mt-0.5">
                {style.font.family} {style.font.size_pt}pt
                {style.font.bold && " bold"}
                {style.font.italic && " italic"}
                {style.alignment !== "left" && ` ${style.alignment}`}
                {style.layout_mode !== "normal" && ` [${style.layout_mode}]`}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Horizontal rules */}
      {Object.keys(layout.horizontal_rules).length > 0 && (
        <div>
          <h3 className="font-semibold text-gray-900 mb-1">
            Horizontal Rules ({Object.keys(layout.horizontal_rules).length})
          </h3>
          <div className="text-xs text-gray-600 font-mono space-y-0.5">
            {Object.entries(layout.horizontal_rules).map(([key, rule]) => (
              <div key={key}>
                {key}: {rule.source_type} ({rule.width_pt}pt)
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
