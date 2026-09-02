"use client";

import { useState, useCallback, useRef } from "react";
import { Upload } from "@/components/Upload";
import { Preview } from "@/components/Preview";
import { Inspector } from "@/components/Inspector";
import { JobDescriptionInput } from "@/components/JobDescriptionInput";
import { TailoringReview } from "@/components/TailoringReview";
import { CopyMode } from "@/components/CopyMode";
import { LayoutSettings } from "@/components/LayoutSettings";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface UploadResult {
  resume_id: string;
  filename: string;
  sections_found: number;
  styles_detected: string[];
  formatting_summary: string[];
}

export default function Home() {
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<UploadResult | null>(null);
  const [customFilename, setCustomFilename] = useState<string>("");
  const [activeTab, setActiveTab] = useState<"jd" | "review" | "copy" | "settings" | "content" | "layout">("jd");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tailoringResult, setTailoringResult] = useState<unknown>(null);
  const [isTailored, setIsTailored] = useState(false);
  const [showDiff, setShowDiff] = useState(true);
  const [previewKey, setPreviewKey] = useState(0);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [lastTailorOpts, setLastTailorOpts] = useState<any>(null);
  const [previewEdits, setPreviewEdits] = useState<Record<string, string>>({});
  // Mirrors previewEdits synchronously. handleSaveAllEdits is a useCallback
  // closed over the `previewEdits` state from whatever render created it --
  // if Preview.tsx flushes a still-debounced edit right before calling
  // onSave (both happen synchronously in the same click handler), the
  // setPreviewEdits() from that flush hasn't been committed to a new
  // render yet, so the onSave closure would still see the old value and
  // silently drop the last edit. Reading from this ref instead guarantees
  // handleSaveAllEdits always sees whatever was written a moment ago,
  // even within the same synchronous handler.
  const previewEditsRef = useRef<Record<string, string>>({});
  const [saveEditLoading, setSaveEditLoading] = useState(false);

  const handleUpload = useCallback(async (file: File) => {
    setLoading(true);
    setError(null);
    setTailoringResult(null);
    setIsTailored(false);

    try {
      const formData = new FormData();
      formData.append("file", file);

      const res = await fetch(`${API}/upload`, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Upload failed");
      }

      const data: UploadResult = await res.json();
      setResumeId(data.resume_id);
      setUploadResult(data);
      // Initialize custom filename from uploaded file, removing extension
      const nameWithoutExt = data.filename.includes(".")
        ? data.filename.substring(0, data.filename.lastIndexOf("."))
        : data.filename;
      setCustomFilename(nameWithoutExt);
      setActiveTab("jd");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const handleTailor = useCallback(
    async (opts: { jdText: string; useLlm: boolean; enforceOnePage: boolean; oneLineBullets: boolean; enforceSingleLine: boolean; maxBulletChars: number }) => {
      if (!resumeId) return;
      setLoading(true);
      setError(null);
      setLastTailorOpts(opts);

      try {
        const res = await fetch(`${API}/tailor/${resumeId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            jd_text: opts.jdText,
            use_llm: opts.useLlm,
            enforce_one_page: opts.enforceOnePage,
            one_line_bullets: opts.oneLineBullets,
            enforce_single_line_bullets: opts.enforceSingleLine,
            max_bullet_chars: opts.maxBulletChars,
          }),
        });

        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "Tailoring failed");
        }

        const data = await res.json();
        setTailoringResult(data);
        setIsTailored(true);
        setShowDiff(true);
        setActiveTab("review");
        setPreviewKey((k) => k + 1);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Tailoring failed");
      } finally {
        setLoading(false);
      }
    },
    [resumeId]
  );

  const handleAcceptReject = useCallback(
    async (bulletId: string, accepted: boolean, resolved: boolean = true) => {
      if (!resumeId) return;

      console.log(`[Accept] Sending: bulletId=${bulletId}, accepted=${accepted}, resolved=${resolved}`);

      try {
        const res = await fetch(`${API}/tailor/${resumeId}/accept`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ bullet_id: bulletId, accepted, resolved }),
        });

        console.log(`[Accept] Response status: ${res.status}`);

        if (!res.ok) {
          const errorData = await res.text();
          console.error(`[Accept] Server error: ${res.status}`, errorData);
          setError(`Accept failed: ${res.status} - ${errorData}`);
          return;
        }

        const updatedResult = await res.json();
        console.log(`[Accept] Updated result:`, updatedResult);
        console.log(`[Accept] Bullet changes:`, updatedResult.bullet_changes?.length, "items");

        setTailoringResult(updatedResult);
        setError(null);
      } catch (err) {
        const errorMsg = err instanceof Error ? err.message : String(err);
        console.error(`[Accept] Network error:`, errorMsg);
        setError(`Accept error: ${errorMsg}`);
      }

      setPreviewKey((k) => k + 1);
    },
    [resumeId]
  );

  const handleSkillChange = useCallback(
    async (changeType: string, category: string, skill: string, accepted: boolean) => {
      if (!resumeId) return;
      const res = await fetch(`${API}/tailor/${resumeId}/skill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ change_type: changeType, category, skill, accepted }),
      });
      if (res.ok) {
        setTailoringResult(await res.json());
      }
      setPreviewKey((k) => k + 1);
    },
    [resumeId]
  );

  const handleFreeformEdit = useCallback(
    async (message: string) => {
      if (!resumeId) return;
      const res = await fetch(`${API}/tailor/${resumeId}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      if (res.ok) {
        const data = await res.json();
        setTailoringResult(data.result);
        setPreviewKey((k) => k + 1);
      }
    },
    [resumeId]
  );

  const handleSkillsAcceptAll = useCallback(
    async (accepted: boolean) => {
      if (!resumeId) return;
      const res = await fetch(`${API}/tailor/${resumeId}/skills/accept-all`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accepted }),
      });
      if (res.ok) {
        setTailoringResult(await res.json());
      }
      setPreviewKey((k) => k + 1);
    },
    [resumeId]
  );

  const handlePreviewEdit = useCallback(
    (bulletId: string, newText: string) => {
      console.log(`[Preview Edit] bulletId=${bulletId}, text="${newText.substring(0, 50)}..."`);
      previewEditsRef.current = { ...previewEditsRef.current, [bulletId]: newText };
      setPreviewEdits(previewEditsRef.current);
    },
    []
  );

  const handleSaveAllEdits = useCallback(
    async () => {
      // Read from the ref, not the `previewEdits` state this callback
      // closed over -- see previewEditsRef's declaration above.
      const edits = previewEditsRef.current;
      if (!resumeId || Object.keys(edits).length === 0) return;

      setSaveEditLoading(true);
      try {
        console.log(`[Save] Saving ${Object.keys(edits).length} preview edits`);

        const response = await fetch(`${API}/tailor/${resumeId}/save-edits`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ edited_bullets: edits }),
        });

        if (!response.ok) throw new Error("Failed to save edits");

        const updated = await response.json();

        // Update tailoring result with new data
        setTailoringResult(updated);
        previewEditsRef.current = {};
        setPreviewEdits({});
        setPreviewKey((k) => k + 1);

        console.log("[Save] Edits saved successfully");
      } catch (err) {
        console.error("[Save] Error:", err);
        setError(err instanceof Error ? err.message : "Failed to save edits");
      } finally {
        setSaveEditLoading(false);
      }
    },
    [resumeId]
  );

  const previewUrl = isTailored
    ? `${API}/preview/${resumeId}/tailored${showDiff ? "?diff=true" : ""}`
    : resumeId
      ? `${API}/preview/${resumeId}`
      : null;

  const getExportUrl = (format: "docx" | "pdf" | "txt", tailored: boolean = false) => {
    if (!resumeId) return null;
    const filename = customFilename || "resume";
    const params = new URLSearchParams({ filename });
    const baseUrl = tailored
      ? `${API}/export/${resumeId}/tailored/${format}`
      : `${API}/export/${resumeId}/${format}`;
    return `${baseUrl}?${params.toString()}`;
  };

  const exportUrl = isTailored ? getExportUrl("docx", true) : getExportUrl("docx", false);

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-3 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold text-gray-900 tracking-tight">Resumax</h1>
        </div>
        <div className="flex items-center gap-4">
          {uploadResult && (
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={customFilename}
                onChange={(e) => setCustomFilename(e.target.value)}
                placeholder="Document name"
                className="text-sm px-2 py-1 border border-gray-300 rounded focus:outline-none focus:ring-2 focus:ring-muted-blue"
              />
              {isTailored && <span className="text-xs text-muted-green whitespace-nowrap">(tailored)</span>}
            </div>
          )}
          {resumeId && (
            <div className="flex items-center gap-1">
              {exportUrl && (
                <a
                  href={exportUrl}
                  download
                  className="px-2.5 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 border border-gray-300 rounded hover:bg-gray-200 transition-colors"
                >
                  .docx
                </a>
              )}
              <a
                href={getExportUrl("pdf", isTailored) || ""}
                download
                className="px-2.5 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 border border-gray-300 rounded hover:bg-gray-200 transition-colors"
              >
                .pdf
              </a>
              <a
                href={getExportUrl("txt", isTailored) || ""}
                download
                className="px-2.5 py-1.5 text-xs font-medium text-gray-600 bg-gray-100 border border-gray-300 rounded hover:bg-gray-200 transition-colors"
              >
                .txt
              </a>
            </div>
          )}
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* Left panel */}
        <div className="w-[440px] border-r border-gray-200 bg-white flex flex-col shrink-0 overflow-hidden">
          <Upload onUpload={handleUpload} loading={loading} error={error} />

          {uploadResult && (
            <>
              <div className="flex border-b border-gray-200 px-4 overflow-x-auto">
                {(["jd", "review", "copy", "settings", "content", "layout"] as const).map((tab) => {
                  const labels: Record<string, string> = { jd: "JD", review: "Review", copy: "Copy", settings: "Settings", content: "Content", layout: "Layout" };
                  return (
                    <button
                      key={tab}
                      onClick={() => setActiveTab(tab)}
                      className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
                        activeTab === tab
                          ? "border-muted-blue text-muted-blue"
                          : "border-transparent text-gray-500 hover:text-gray-700"
                      }`}
                    >
                      {labels[tab]}
                    </button>
                  );
                })}
              </div>

              <div className="flex-1 overflow-auto">
                {activeTab === "jd" && (
                  <JobDescriptionInput
                    onTailor={handleTailor}
                    loading={loading}
                    apiUrl={API}
                    resumeId={resumeId!}
                  />
                )}
                {activeTab === "review" && tailoringResult != null && (
                  <TailoringReview
                    result={tailoringResult}
                    onAcceptReject={handleAcceptReject}
                    onSkillChange={handleSkillChange}
                    onSkillsAcceptAll={handleSkillsAcceptAll}
                    onRedo={lastTailorOpts ? () => handleTailor(lastTailorOpts) : undefined}
                    onFreeformEdit={handleFreeformEdit}
                    loading={loading}
                  />
                )}
                {activeTab === "copy" && resumeId && (
                  <CopyMode resumeId={resumeId} apiUrl={API} isTailored={isTailored} />
                )}
                {activeTab === "settings" && resumeId && (
                  <LayoutSettings
                    resumeId={resumeId}
                    apiUrl={API}
                    onUpdate={() => setPreviewKey((k) => k + 1)}
                  />
                )}
                {activeTab === "content" && resumeId && (
                  <Inspector resumeId={resumeId} tab="content" apiUrl={API} />
                )}
                {activeTab === "layout" && resumeId && (
                  <Inspector resumeId={resumeId} tab="layout" apiUrl={API} />
                )}
              </div>
            </>
          )}
        </div>

        {/* Right panel: Live preview */}
        <div className="flex-1 bg-gray-200 overflow-auto flex flex-col items-center">
          {/* Diff toggle */}
          {isTailored && (
            <div className="flex items-center gap-2 pt-4 pb-1">
              <button
                onClick={() => { setShowDiff((d) => !d); setPreviewKey((k) => k + 1); }}
                className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
                  showDiff
                    ? "bg-gray-200 text-gray-700 border border-gray-300"
                    : "bg-white text-gray-500 border border-gray-300 hover:bg-gray-50"
                }`}
              >
                {showDiff ? "Showing Changes" : "Show Changes"}
              </button>
              {showDiff && (
                <span className="text-[10px] text-gray-400">
                  <span className="inline-block w-2.5 h-2.5 rounded-sm bg-muted-green mr-0.5 align-middle" /> added
                  <span className="inline-block w-2.5 h-2.5 rounded-sm bg-muted-red ml-2 mr-0.5 align-middle" /> removed
                </span>
              )}
            </div>
          )}
          <div className="flex-1 w-full flex justify-center py-2">
            {previewUrl ? (
              <Preview
                key={previewKey}
                resumeId={resumeId!}
                apiUrl={API}
                previewUrl={previewUrl}
                editable={activeTab === "review"}
                onTextEdit={handlePreviewEdit}
                onSave={activeTab === "review" ? handleSaveAllEdits : undefined}
                hasEdits={Object.keys(previewEdits).length > 0}
                saveLoading={saveEditLoading}
              />
            ) : (
              <div className="flex items-center justify-center h-full text-gray-400">
                Upload a resume to see the preview
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
