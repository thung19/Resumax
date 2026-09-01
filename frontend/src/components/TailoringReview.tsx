"use client";

import { useMemo, useState } from "react";

interface BulletChange {
  bullet_id: string;
  original_text: string;
  tailored_text: string;
  action: string;
  source_fact_ids: string[];
  target_keywords: string[];
  reason: string;
  accepted: boolean;
  resolved: boolean;
}

interface TailoringResultData {
  job_title: string;
  bullet_changes: BulletChange[];
  required_skill_coverage: number;
  technical_keyword_coverage: number;
  responsibility_coverage: number;
  required_skills_matched?: string[];
  required_skills_missing?: string[];
  technical_keywords_matched?: string[];
  technical_keywords_missing?: string[];
  responsibilities_matched?: string[];
  responsibilities_missing?: string[];
  reordered_skills: Record<string, string[]>;
  reorder_accepted: Record<string, boolean>;
  added_skills: Record<string, string[]>;
  additions_accepted: Record<string, boolean>;
  fit_report: { fits: boolean; page_count: number; actions_taken: string[] } | null;
  debug_log?: string[];
  planning_used?: boolean;
  planning_error?: string;
  planning_duration_ms?: number;

  // Simplified metrics (from new categorized requirements system)
  skills_matched_coverage?: number;     // % of technical_requirements matched
  activities_matched_coverage?: number; // % of deliverables matched
  skills_matched?: string[];
  skills_missing?: string[];
  activities_matched?: string[];
  activities_missing?: string[];

  // Phase 4: Two-tier coverage metrics
  ats_coverage?: number;        // 0.0-1.0: What ATS will literally find
  human_coverage?: number;      // 0.0-1.0: What humans would understand
  coverage_gap?: number;        // 0.0-1.0: Difference (human - ats)
}

interface TailoringReviewProps {
  result: unknown;
  onAcceptReject: (bulletId: string, accepted: boolean, resolved?: boolean) => void;
  onSkillChange?: (changeType: string, category: string, skill: string, accepted: boolean) => void;
  onSkillsAcceptAll?: (accepted: boolean) => void;
  onRedo?: () => void;
  onFreeformEdit?: (message: string) => Promise<void>;
  loading?: boolean;
}

export function TailoringReview({ result, onAcceptReject, onSkillChange, onSkillsAcceptAll, onRedo, onFreeformEdit, loading }: TailoringReviewProps) {
  const [editMessage, setEditMessage] = useState("");
  const [editLoading, setEditLoading] = useState(false);
  const data = result as TailoringResultData;

  const rewrites = data.bullet_changes.filter((c) => c.action === "rewrite");
  const keeps = data.bullet_changes.filter((c) => c.action === "keep");

  // Use backend resolved field — no local state needed
  const pending = rewrites.filter((c) => !c.resolved);
  const done = rewrites.filter((c) => c.resolved);

  const handleAction = (bulletId: string, accepted: boolean) => {
    onAcceptReject(bulletId, accepted);
  };

  return (
    <div className="p-4 space-y-4 text-sm">
      {/* Coverage metrics with breakdown */}
      <div className="space-y-2">
        <div className="grid grid-cols-2 gap-2">
          {data.skills_matched_coverage !== undefined ? (
            <MetricCard label="Skills Matched" value={data.skills_matched_coverage} />
          ) : (
            <MetricCard label="Required Skills" value={data.required_skill_coverage} />
          )}
          {data.activities_matched_coverage !== undefined ? (
            <MetricCard label="Activities Matched" value={data.activities_matched_coverage} />
          ) : (
            <MetricCard label="Technical Keywords" value={data.technical_keyword_coverage} />
          )}
        </div>

        {/* ATS Visibility — only show when there's a meaningful gap (>5%) */}
        {(data.coverage_gap ?? 0) > 0.05 && (
          <div className="bg-blue-50 rounded-lg border border-blue-200 p-3 text-xs">
            <div className="font-semibold text-blue-900 mb-2">ATS Visibility Gap</div>
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-blue-800">ATS will find:</span>
                <span className="font-bold text-blue-900">{Math.round((data.ats_coverage ?? 0) * 100)}%</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-blue-800">Humans understand:</span>
                <span className="font-bold text-blue-900">{Math.round((data.human_coverage ?? 0) * 100)}%</span>
              </div>
              <div className="mt-2 pt-2 border-t border-blue-200">
                <div className="text-blue-700">
                  💡 Gap of {Math.round((data.coverage_gap ?? 0) * 100)}%: Humans recognize concepts using contextual clues (like "Agile" from "Scrum sprints"), but ATS might miss these without explicit keywords. Add literal keywords to improve ATS matching.
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Detailed coverage breakdown */}
        {(data.skills_matched || data.skills_missing ||
          data.activities_matched || data.activities_missing ||
          data.required_skills_matched || data.required_skills_missing ||
          data.technical_keywords_matched || data.technical_keywords_missing ||
          data.responsibilities_matched || data.responsibilities_missing) && (
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            {/* Skills (from new categorized system) */}
            {(data.skills_matched || data.skills_missing) && (
              <div className="border-b border-gray-100 p-3">
                <div className="font-semibold text-gray-900 mb-2 text-xs">
                  Skills <span className="text-gray-500 font-normal">({Math.round((data.skills_matched_coverage ?? 0) * 100)}%)</span>
                </div>
                <div className="space-y-1.5">
                  {/* Matched - Green pills */}
                  {(data.skills_matched?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {data.skills_matched!.map((skill) => (
                        <span key={skill} className="inline-flex items-center gap-1 bg-muted-green text-muted-green px-2 py-1 rounded text-xs font-medium">
                          <span>✓</span>
                          <span>{skill}</span>
                        </span>
                      ))}
                    </div>
                  )}
                  {/* Missing - Red pills */}
                  {(data.skills_missing?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {data.skills_missing!.map((skill) => (
                        <span key={skill} className="inline-flex items-center gap-1 bg-muted-red text-muted-red px-2 py-1 rounded text-xs font-medium">
                          <span>✕</span>
                          <span>{skill}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Activities (from new categorized system) */}
            {(data.activities_matched || data.activities_missing) && (
              <div className="border-b border-gray-100 p-3">
                <div className="font-semibold text-gray-900 mb-2 text-xs">
                  Activities <span className="text-gray-500 font-normal">({Math.round((data.activities_matched_coverage ?? 0) * 100)}%)</span>
                </div>
                <div className="space-y-1">
                  {/* Matched - Green items */}
                  {(data.activities_matched?.length ?? 0) > 0 && (
                    <div>
                      {data.activities_matched!.slice(0, 6).map((activity, idx) => (
                        <div key={idx} className="flex gap-2 text-xs mb-1">
                          <span className="text-muted-green font-bold mt-0.5">✓</span>
                          <span className="text-gray-700 flex-1">{activity.substring(0, 60)}{activity.length > 60 ? "..." : ""}</span>
                        </div>
                      ))}
                      {(data.activities_matched?.length ?? 0) > 6 && (
                        <div className="text-xs text-gray-500 px-2">
                          +{data.activities_matched!.length - 6} more matched
                        </div>
                      )}
                    </div>
                  )}
                  {/* Missing - Red items */}
                  {(data.activities_missing?.length ?? 0) > 0 && (
                    <div>
                      {data.activities_missing!.slice(0, 6).map((activity, idx) => (
                        <div key={idx} className="flex gap-2 text-xs mb-1">
                          <span className="text-muted-red font-bold mt-0.5">✕</span>
                          <span className="text-gray-700 flex-1">{activity.substring(0, 60)}{activity.length > 60 ? "..." : ""}</span>
                        </div>
                      ))}
                      {(data.activities_missing?.length ?? 0) > 6 && (
                        <div className="text-xs text-gray-500 px-2">
                          +{data.activities_missing!.length - 6} more missing
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Required Skills (legacy) — only show if new metrics not available */}
            {!data.skills_matched && (data.required_skills_matched || data.required_skills_missing) && (
              <div className="border-b border-gray-100 p-3">
                <div className="font-semibold text-gray-900 mb-2 text-xs">
                  Required Skills <span className="text-gray-500 font-normal">({Math.round(data.required_skill_coverage * 100)}%)</span>
                </div>
                <div className="space-y-1.5">
                  {/* Matched - Green pills */}
                  {(data.required_skills_matched?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {data.required_skills_matched!.map((skill) => (
                        <span key={skill} className="inline-flex items-center gap-1 bg-muted-green text-muted-green px-2 py-1 rounded text-xs font-medium">
                          <span>✓</span>
                          <span>{skill}</span>
                        </span>
                      ))}
                    </div>
                  )}
                  {/* Missing - Red pills */}
                  {(data.required_skills_missing?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {data.required_skills_missing!.map((skill) => (
                        <span key={skill} className="inline-flex items-center gap-1 bg-muted-red text-muted-red px-2 py-1 rounded text-xs font-medium">
                          <span>✕</span>
                          <span>{skill}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Technical Keywords (legacy) — only show if new metrics not available */}
            {!data.activities_matched && (data.technical_keywords_matched || data.technical_keywords_missing) && (
              <div className="border-b border-gray-100 p-3">
                <div className="font-semibold text-gray-900 mb-2 text-xs">
                  Technical Keywords <span className="text-gray-500 font-normal">({Math.round(data.technical_keyword_coverage * 100)}%)</span>
                </div>
                <div className="space-y-1.5">
                  {/* Matched - Green pills */}
                  {(data.technical_keywords_matched?.length ?? 0) > 0 && (
                    <div>
                      <div className="flex flex-wrap gap-1.5 mb-1.5">
                        {data.technical_keywords_matched!.slice(0, 12).map((keyword) => (
                          <span key={keyword} className="inline-flex items-center gap-1 bg-muted-green text-muted-green px-2 py-1 rounded text-xs font-medium">
                            <span>✓</span>
                            <span>{keyword}</span>
                          </span>
                        ))}
                      </div>
                      {(data.technical_keywords_matched?.length ?? 0) > 12 && (
                        <div className="text-xs text-gray-500 px-2">
                          +{data.technical_keywords_matched!.length - 12} more matched keywords
                        </div>
                      )}
                    </div>
                  )}
                  {/* Missing - Red pills */}
                  {(data.technical_keywords_missing?.length ?? 0) > 0 && (
                    <div>
                      <div className="flex flex-wrap gap-1.5 mb-1.5">
                        {data.technical_keywords_missing!.slice(0, 12).map((keyword) => (
                          <span key={keyword} className="inline-flex items-center gap-1 bg-muted-red text-muted-red px-2 py-1 rounded text-xs font-medium">
                            <span>✕</span>
                            <span>{keyword}</span>
                          </span>
                        ))}
                      </div>
                      {(data.technical_keywords_missing?.length ?? 0) > 12 && (
                        <div className="text-xs text-gray-500 px-2">
                          +{data.technical_keywords_missing!.length - 12} more missing keywords
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Responsibilities (legacy) — only show if new metrics not available */}
            {!data.activities_matched && (data.responsibilities_matched || data.responsibilities_missing) && (
              <div className="p-3">
                <div className="font-semibold text-gray-900 mb-2 text-xs">
                  Responsibilities <span className="text-gray-500 font-normal">({Math.round(data.responsibility_coverage * 100)}%)</span>
                </div>
                <div className="space-y-1">
                  {/* Matched - Green items */}
                  {(data.responsibilities_matched?.length ?? 0) > 0 && (
                    <div>
                      {data.responsibilities_matched!.slice(0, 5).map((resp, idx) => (
                        <div key={idx} className="flex gap-2 text-xs mb-1">
                          <span className="text-muted-green font-bold mt-0.5">✓</span>
                          <span className="text-gray-700 flex-1">{resp.substring(0, 60)}{resp.length > 60 ? "..." : ""}</span>
                        </div>
                      ))}
                      {(data.responsibilities_matched?.length ?? 0) > 5 && (
                        <div className="text-xs text-gray-500 px-2">
                          +{data.responsibilities_matched!.length - 5} more covered
                        </div>
                      )}
                    </div>
                  )}
                  {/* Missing - Red items */}
                  {(data.responsibilities_missing?.length ?? 0) > 0 && (
                    <div>
                      {data.responsibilities_missing!.slice(0, 5).map((resp, idx) => (
                        <div key={idx} className="flex gap-2 text-xs mb-1">
                          <span className="text-muted-red font-bold mt-0.5">✕</span>
                          <span className="text-gray-700 flex-1">{resp.substring(0, 60)}{resp.length > 60 ? "..." : ""}</span>
                        </div>
                      ))}
                      {(data.responsibilities_missing?.length ?? 0) > 5 && (
                        <div className="text-xs text-gray-500 px-2">
                          +{data.responsibilities_missing!.length - 5} more not covered
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Fit report */}
      {data.fit_report && (
        <div className={`rounded px-3 py-2 text-xs ${data.fit_report.fits ? "bg-muted-green text-muted-green" : "bg-muted-red text-muted-red"}`}>
          {data.fit_report.fits
            ? `Fits on ${data.fit_report.page_count} page${data.fit_report.page_count > 1 ? "s" : ""}`
            : `Overflow: ${data.fit_report.page_count} pages`}
          {data.fit_report.actions_taken.length > 0 && (
            <span className="text-gray-500 ml-1">
              ({data.fit_report.actions_taken.length} adjustments made)
            </span>
          )}
        </div>
      )}



      {/* Summary bar */}
      <div className="flex gap-3 text-xs items-center bg-white rounded-lg px-3 py-2 border border-gray-200">
        <span className="font-medium text-muted-blue">{pending.length} pending</span>
        <span className="font-medium text-muted-green">{done.filter((c) => c.accepted).length} accepted</span>
        <span className="font-medium text-muted-red">{done.filter((c) => !c.accepted).length} rejected</span>
        <span className="text-gray-500">{keeps.length} kept</span>
        <div className="ml-auto flex gap-1">
          {onRedo && (
            <button
              onClick={onRedo}
              disabled={loading}
              className="px-2.5 py-1 text-[10px] font-medium bg-gray-100 text-gray-700 border border-gray-300 rounded hover:bg-gray-200 disabled:opacity-50"
            >
              {loading ? "Tailoring..." : "Redo"}
            </button>
          )}
          {pending.length > 0 && (
            <button
              onClick={() => {
                pending.forEach((c) => handleAction(c.bullet_id, true));
              }}
              className="px-2.5 py-1 text-[10px] font-medium bg-muted-green text-muted-green rounded hover:opacity-80"
            >
              Accept all
            </button>
          )}
        </div>
      </div>

      {/* Pending changes — VS Code-style diff */}
      {pending.length > 0 && (
        <div className="space-y-2">
          {pending.map((change) => (
            <DiffCard
              key={change.bullet_id}
              change={change}
              onAccept={() => handleAction(change.bullet_id, true)}
              onReject={() => handleAction(change.bullet_id, false)}
            />
          ))}
        </div>
      )}

      {/* Resolved changes — collapsed */}
      {done.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold text-gray-600 mb-1">
            Resolved ({done.length})
          </h3>
          <div className="space-y-1">
            {done.map((c) => (
              <ResolvedRow
                key={c.bullet_id}
                change={c}
                onUndo={() => {
                  onAcceptReject(c.bullet_id, true, false);
                }}
              />
            ))}
          </div>
        </div>
      )}

      {/* Skills additions */}
      {Object.keys(data.added_skills || {}).length > 0 && (() => {
        const hasPendingAdditions = Object.entries(data.added_skills).some(([cat, skills]) =>
          skills.some((skill) => !(((`${cat}:${skill}`) in (data.additions_accepted || {}))))
        );
        const hasPendingReorders = Object.keys(data.reordered_skills).some((cat) =>
          !(cat in (data.reorder_accepted || {}))
        );
        const hasPending = hasPendingAdditions || hasPendingReorders;
        return (
        <div className="border-t border-gray-200 pt-3">
          <div className="flex items-center justify-between mb-2">
            <h3 className="font-semibold text-gray-900">Skills Added</h3>
            {hasPending && onSkillsAcceptAll && (
              <button
                onClick={() => onSkillsAcceptAll(true)}
                className="px-2 py-0.5 text-[10px] bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
              >
                Accept all skills
              </button>
            )}
          </div>
          <div className="space-y-1.5">
            {Object.entries(data.added_skills).flatMap(([cat, skills]) =>
              skills.map((skill) => {
                const key = `${cat}:${skill}`;
                const accepted = data.additions_accepted?.[key] ?? true;
                const resolved = key in (data.additions_accepted || {});
                return (
                  <div key={key} className={`flex items-center gap-2 px-2.5 py-1.5 rounded text-xs border ${resolved ? (accepted ? "bg-muted-green border-muted-green" : "bg-gray-100 border-gray-200 opacity-60") : "bg-muted-blue border-muted-blue"}`}>
                    <span className="font-semibold text-gray-800">{cat}:</span>
                    <span className="text-gray-700 flex-1">+ {skill}</span>
                    {!resolved ? (
                      <div className="flex gap-1">
                        <button onClick={() => onSkillChange?.("addition", cat, skill, true)}
                          className="px-1.5 py-0.5 text-[10px] bg-gray-100 text-muted-green border border-gray-200 rounded hover:opacity-80">
                          Accept
                        </button>
                        <button onClick={() => onSkillChange?.("addition", cat, skill, false)}
                          className="px-1.5 py-0.5 text-[10px] bg-gray-100 text-muted-red border border-gray-200 rounded hover:opacity-80">
                          Reject
                        </button>
                      </div>
                    ) : (
                      <span className={`text-[10px] ${accepted ? "text-muted-green" : "text-muted-red"}`}>
                        {accepted ? "✓" : "✗"}
                      </span>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
        );
      })()}

      {/* Skills reordering */}
      {Object.keys(data.reordered_skills).length > 0 && (
        <div className="border-t border-gray-200 pt-3">
          <h3 className="font-semibold text-gray-900 mb-2">Skills Reordered</h3>
          {Object.entries(data.reordered_skills).map(([cat, skills]) => {
            const accepted = data.reorder_accepted?.[cat] ?? true;
            const resolved = cat in (data.reorder_accepted || {});
            return (
              <div key={cat} className={`text-xs mb-2 p-2.5 rounded border ${resolved ? (accepted ? "bg-muted-green border-muted-green text-gray-700" : "bg-gray-100 border-gray-200 text-gray-500 opacity-60") : "bg-muted-blue border-muted-blue text-gray-700"}`}>
                <div className="flex items-center justify-between mb-0.5">
                  <span className="font-semibold text-gray-800">{cat}</span>
                  {!resolved ? (
                    <div className="flex gap-1">
                      <button onClick={() => onSkillChange?.("reorder", cat, "", true)}
                        className="px-1.5 py-0.5 text-[10px] bg-gray-100 text-muted-green border border-gray-200 rounded hover:opacity-80">
                        Accept
                      </button>
                      <button onClick={() => onSkillChange?.("reorder", cat, "", false)}
                        className="px-1.5 py-0.5 text-[10px] bg-gray-100 text-muted-red border border-gray-200 rounded hover:opacity-80">
                        Reject
                      </button>
                    </div>
                  ) : (
                    <span className={`text-[10px] ${accepted ? "text-muted-green" : "text-muted-red"}`}>
                      {accepted ? "✓ accepted" : "✗ rejected"}
                    </span>
                  )}
                </div>
                <div>{skills.join(", ")}</div>
              </div>
            );
          })}
        </div>
      )}

      {/* Debug panel */}
      {(data.debug_log?.length || data.planning_error) && (
        <DebugPanel data={data} />
      )}

      {/* Freeform edit input */}
      {onFreeformEdit && (
        <div className="border-t border-gray-200 pt-3">
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (!editMessage.trim() || editLoading) return;
              setEditLoading(true);
              try {
                await onFreeformEdit(editMessage.trim());
                setEditMessage("");
              } finally {
                setEditLoading(false);
              }
            }}
            className="flex gap-2"
          >
            <input
              type="text"
              value={editMessage}
              onChange={(e) => setEditMessage(e.target.value)}
              placeholder="Tell the AI to edit the resume..."
              disabled={editLoading}
              className="flex-1 px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-blue-400 disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={!editMessage.trim() || editLoading}
              className="px-3 py-2 text-sm font-medium bg-gray-900 text-white rounded-lg hover:bg-gray-800 disabled:opacity-40 transition-colors shrink-0"
            >
              {editLoading ? "Editing..." : "Send"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}

// --- Debug panel ---

function DebugPanel({ data }: { data: TailoringResultData }) {
  const [open, setOpen] = useState(false);
  const log = data.debug_log || [];
  const reverted = log.filter((l) => l.startsWith("REVERTED"));
  const accepted = log.filter((l) => l.startsWith("ACCEPTED"));

  return (
    <div className="border-t border-gray-200 pt-3">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 text-xs font-medium text-gray-500 hover:text-gray-700"
      >
        <span>{open ? "▼" : "▶"}</span>
        Debug ({reverted.length} reverted, {accepted.length} accepted)
        {data.planning_error && (
          <span className="text-muted-red ml-1">LLM error</span>
        )}
      </button>
      {open && (
        <div className="mt-2 space-y-1 text-[11px] font-mono bg-gray-900 text-gray-300 rounded p-3 max-h-80 overflow-auto">
          {data.planning_error && (
            <div className="text-red-400">ERROR: {data.planning_error}</div>
          )}
          {data.planning_duration_ms != null && (
            <div className="text-gray-500">
              LLM call: {data.planning_duration_ms}ms
              {data.planning_used ? "" : " (not used)"}
            </div>
          )}
          {log.map((line, i) => {
            let color = "text-gray-400";
            if (line.startsWith("REVERTED")) color = "text-red-400";
            else if (line.startsWith("ACCEPTED")) color = "text-green-400";
            else if (line.startsWith("LLM")) color = "text-blue-400";
            return (
              <div key={i} className={color}>
                {line}
              </div>
            );
          })}
          {log.length === 0 && !data.planning_error && (
            <div className="text-gray-500">No debug info available</div>
          )}
        </div>
      )}
    </div>
  );
}

// --- VS Code-style diff card ---

function DiffCard({
  change,
  onAccept,
  onReject,
}: {
  change: BulletChange;
  onAccept: () => void;
  onReject: () => void;
}) {
  const diff = useMemo(() => computeWordDiff(change.original_text, change.tailored_text), [change.original_text, change.tailored_text]);

  return (
    <div className="border border-gray-300 rounded-lg overflow-hidden bg-white">
      {/* Header bar */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-gray-50 border-b border-gray-200">
        <span className="text-[10px] text-gray-400 font-mono">{change.bullet_id}</span>
        <div className="flex gap-1">
          <button
            onClick={onAccept}
            className="px-2.5 py-1 text-[11px] font-medium rounded border transition-colors bg-muted-green text-muted-green border-muted-green hover:opacity-80"
          >
            Accept
          </button>
          <button
            onClick={onReject}
            className="px-2.5 py-1 text-[11px] font-medium rounded border transition-colors bg-muted-red text-muted-red border-muted-red hover:opacity-80"
          >
            Reject
          </button>
        </div>
      </div>

      {/* Diff view */}
      <div className="font-mono text-[11px] leading-5">
        {/* Removed line */}
        <div className="bg-muted-red border-l-2 border-muted-red px-3 py-1 flex">
          <span className="text-muted-red select-none w-4 shrink-0 opacity-60">−</span>
          <span>{diff.removed}</span>
        </div>
        {/* Added line */}
        <div className="bg-muted-green border-l-2 border-muted-green px-3 py-1 flex">
          <span className="text-muted-green select-none w-4 shrink-0 opacity-60">+</span>
          <span>{diff.added}</span>
        </div>
      </div>

      {/* Reason + keywords */}
      {(change.reason || change.target_keywords.length > 0) && (
        <div className="px-3 py-1.5 bg-gray-50 border-t border-gray-200">
          {change.reason && (
            <div className="text-[11px] text-gray-700 leading-snug">{change.reason}</div>
          )}
          {change.target_keywords.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {change.target_keywords.map((kw, i) => (
                <span key={i} className="px-1.5 py-0.5 text-[9px] font-medium bg-muted-blue text-muted-blue rounded">
                  {kw}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// --- Collapsed resolved row ---

function ResolvedRow({
  change,
  onUndo,
}: {
  change: BulletChange;
  onUndo: () => void;
}) {
  const accepted = change.accepted;
  const text = accepted ? change.tailored_text : change.original_text;

  return (
    <div className={`flex items-start gap-2 px-2.5 py-1.5 rounded text-[11px] border ${
      accepted
        ? "bg-muted-green border-muted-green"
        : "bg-muted-red border-muted-red"
    }`}>
      <span className={`shrink-0 mt-0.5 font-bold ${accepted ? "text-muted-green" : "text-muted-red"}`}>
        {accepted ? "✓" : "✗"}
      </span>
      <span className="text-gray-700 flex-1 px-1 py-0.5">
        {text.length > 90 ? text.slice(0, 90) + "..." : text}
      </span>
      <button
        onClick={onUndo}
        className="text-[10px] text-gray-500 hover:text-gray-800 shrink-0 underline"
      >
        undo
      </button>
    </div>
  );
}

// --- Metric card ---

function MetricCard({ label, value }: { label: string; value: number }) {
  const pct = Math.round(value * 100);
  const color =
    pct >= 70 ? "text-muted-green bg-muted-green" :
    pct >= 40 ? "text-muted-amber bg-muted-amber" :
    "text-muted-red bg-muted-red";

  return (
    <div className={`rounded p-2 text-center ${color}`}>
      <div className="text-lg font-bold">{pct}%</div>
      <div className="text-xs">{label}</div>
    </div>
  );
}

// --- Word-level diff with highlighting ---

interface DiffResult {
  removed: React.ReactNode;
  added: React.ReactNode;
}

function computeWordDiff(original: string, tailored: string): DiffResult {
  const origWords = original.split(/(\s+)/);
  const tailWords = tailored.split(/(\s+)/);

  // Simple LCS-based word diff
  const origSet = new Set(origWords.filter((w) => w.trim()));
  const tailSet = new Set(tailWords.filter((w) => w.trim()));

  const removedParts = origWords.map((word, i) => {
    if (!word.trim()) return <span key={i}>{word}</span>;
    if (!tailSet.has(word)) {
      return <span key={i} className="line-through rounded-sm px-0.5" style={{background: 'var(--muted-red-mid)', color: 'var(--muted-red)'}}>{word}</span>;
    }
    return <span key={i}>{word}</span>;
  });

  const addedParts = tailWords.map((word, i) => {
    if (!word.trim()) return <span key={i}>{word}</span>;
    if (!origSet.has(word)) {
      return <span key={i} className="font-medium rounded-sm px-0.5" style={{background: 'var(--muted-green-mid)', color: '#374151'}}>{word}</span>;
    }
    return <span key={i}>{word}</span>;
  });

  return {
    removed: <>{removedParts}</>,
    added: <>{addedParts}</>,
  };
}
