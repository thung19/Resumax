"use client";

import { useEffect, useState, useCallback } from "react";

interface LayoutSettingsProps {
  resumeId: string;
  apiUrl: string;
  onUpdate: () => void;
}

interface Settings {
  margins: { top: number; bottom: number; left: number; right: number };
  font_family: string;
  sizes: { name_pt: number; heading_pt: number; body_pt: number };
  spacing: { line_spacing: number; spacer_sizes_pt: number[] };
  page: { width: number; height: number };
  element_count: number;
  hyperlinks: Record<string, string>;
}

interface TemplateInfo {
  id: string;
  name: string;
  description: string;
  font: string;
  size: number;
  margins: { top: number; bottom: number; left: number; right: number };
}

export function LayoutSettings({ resumeId, apiUrl, onUpdate }: LayoutSettingsProps) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [applyingTemplate, setApplyingTemplate] = useState(false);

  useEffect(() => {
    fetch(`${apiUrl}/layout/${resumeId}/settings`)
      .then((r) => r.json())
      .then(setSettings);
    fetch(`${apiUrl}/templates`)
      .then((r) => r.json())
      .then((data) => setTemplates(data.templates || []));
  }, [resumeId, apiUrl]);

  const save = useCallback(
    async (updates: Record<string, unknown>) => {
      setSaving(true);
      try {
        const res = await fetch(`${apiUrl}/layout/${resumeId}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(updates),
        });
        if (res.ok) {
          setDirty(true);
          // Reload settings
          const newSettings = await fetch(`${apiUrl}/layout/${resumeId}/settings`).then((r) => r.json());
          setSettings(newSettings);
          onUpdate();
        }
      } finally {
        setSaving(false);
      }
    },
    [resumeId, apiUrl, onUpdate]
  );

  const applyTemplate = useCallback(
    async (templateId: string) => {
      setApplyingTemplate(true);
      try {
        const res = await fetch(`${apiUrl}/templates/apply/${resumeId}/${templateId}`, {
          method: "POST",
        });
        if (res.ok) {
          // Reload settings
          const newSettings = await fetch(`${apiUrl}/layout/${resumeId}/settings`).then((r) => r.json());
          setSettings(newSettings);
          setDirty(true);
          onUpdate();
        }
      } finally {
        setApplyingTemplate(false);
      }
    },
    [resumeId, apiUrl, onUpdate]
  );

  const saveAsTemplate = useCallback(
    async () => {
      const name = prompt("Template name:");
      if (!name) return;
      const desc = prompt("Description (optional):") || "";
      const res = await fetch(`${apiUrl}/templates/save/${resumeId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description: desc }),
      });
      if (res.ok) {
        // Reload templates
        const data = await fetch(`${apiUrl}/templates`).then((r) => r.json());
        setTemplates(data.templates || []);
      }
    },
    [resumeId, apiUrl]
  );

  if (!settings) return <div className="p-4 text-sm text-gray-400">Loading...</div>;

  return (
    <div className="p-4 space-y-5 text-sm">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-gray-900">Layout Settings</h3>
        {dirty && <span className="text-xs text-muted-green font-medium">Preview updated</span>}
      </div>

      {/* Template selector */}
      {templates.length > 0 && (
        <Section title="Format Templates">
          <div className="space-y-1.5">
            {templates.map((t) => (
              <div
                key={t.id}
                className="flex items-center justify-between bg-white border border-gray-200 rounded px-2.5 py-1.5"
              >
                <div>
                  <div className="text-xs font-medium text-gray-800">{t.name}</div>
                  <div className="text-[10px] text-gray-500">
                    {t.font} {t.size}pt · margins {t.margins.left}/{t.margins.right}"
                  </div>
                </div>
                <button
                  onClick={() => applyTemplate(t.id)}
                  disabled={applyingTemplate}
                  className="px-2 py-0.5 text-[10px] font-medium bg-muted-blue text-muted-blue border border-muted-blue rounded hover:opacity-80 disabled:opacity-50"
                >
                  {applyingTemplate ? "..." : "Apply"}
                </button>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Save current as template */}
      <button
        onClick={saveAsTemplate}
        className="w-full px-2 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 border border-gray-200 rounded hover:bg-gray-200"
      >
        Save current formatting as template
      </button>

      {/* Page info */}
      <div className="text-xs text-gray-400">
        Page: {settings.page.width}" x {settings.page.height}" |{" "}
        {settings.element_count} elements captured
      </div>

      {/* Margins */}
      <Section title="Margins (inches)">
        <div className="grid grid-cols-2 gap-2">
          <NumberInput label="Top" value={settings.margins.top} step={0.05}
            onChange={(v) => save({ margin_top: v })} />
          <NumberInput label="Bottom" value={settings.margins.bottom} step={0.05}
            onChange={(v) => save({ margin_bottom: v })} />
          <NumberInput label="Left" value={settings.margins.left} step={0.05}
            onChange={(v) => save({ margin_left: v })} />
          <NumberInput label="Right" value={settings.margins.right} step={0.05}
            onChange={(v) => save({ margin_right: v })} />
        </div>
      </Section>

      {/* Font */}
      <Section title="Font">
        <div className="space-y-2">
          <div>
            <label className="text-xs text-gray-500">Family</label>
            <select
              value={settings.font_family}
              onChange={(e) => save({ font_family: e.target.value })}
              className="w-full mt-0.5 text-xs border border-gray-300 rounded px-2 py-1"
            >
              {["Garamond", "Times New Roman", "Arial", "Calibri", "Cambria", "Georgia", "Helvetica", "Palatino"].map((f) => (
                <option key={f} value={f}>{f}</option>
              ))}
            </select>
          </div>
        </div>
      </Section>

      {/* Font Sizes */}
      <Section title="Font Sizes (pt)">
        <div className="grid grid-cols-3 gap-2">
          <NumberInput label="Name" value={settings.sizes.name_pt} step={0.5} min={14} max={40}
            onChange={(v) => save({ name_size_pt: v })} />
          <NumberInput label="Heading" value={settings.sizes.heading_pt} step={0.5} min={8} max={20}
            onChange={(v) => save({ heading_size_pt: v })} />
          <NumberInput label="Body" value={settings.sizes.body_pt} step={0.5} min={8} max={14}
            onChange={(v) => save({ body_size_pt: v })} />
        </div>
      </Section>

      {/* Spacing */}
      <Section title="Spacing">
        <div className="space-y-2">
          <NumberInput label="Line spacing" value={settings.spacing.line_spacing} step={0.05} min={0.8} max={2.0}
            onChange={(v) => save({ line_spacing: v })} />
          {settings.spacing.spacer_sizes_pt.length > 0 && (
            <NumberInput
              label="Gap size (pt)"
              value={settings.spacing.spacer_sizes_pt[0]}
              step={0.5}
              min={1}
              max={20}
              onChange={(v) => save({ spacer_size_pt: v })}
            />
          )}
        </div>
      </Section>

      {/* Hyperlinks */}
      {Object.keys(settings.hyperlinks).length > 0 && (
        <Section title="Hyperlinks">
          <div className="space-y-1">
            {Object.entries(settings.hyperlinks).map(([id, url]) => (
              <div key={id} className="text-xs text-gray-600 truncate">
                {url}
              </div>
            ))}
          </div>
        </Section>
      )}

      {saving && (
        <div className="text-xs text-gray-400">Saving...</div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs font-medium text-gray-600 mb-1.5">{title}</div>
      {children}
    </div>
  );
}

function NumberInput({
  label,
  value,
  step = 0.1,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  step?: number;
  min?: number;
  max?: number;
  onChange: (value: number) => void;
}) {
  return (
    <div>
      <label className="text-[10px] text-gray-400 block">{label}</label>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        max={max}
        onChange={(e) => {
          const v = parseFloat(e.target.value);
          if (!isNaN(v)) onChange(v);
        }}
        className="w-full text-xs border border-gray-300 rounded px-2 py-1 mt-0.5"
      />
    </div>
  );
}
