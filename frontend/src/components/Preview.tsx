"use client";

import { useEffect, useRef, useState } from "react";

interface PreviewProps {
  resumeId: string;
  apiUrl: string;
  previewUrl?: string;
  editable?: boolean;
  onTextEdit?: (bulletId: string, newText: string) => void;
  onSave?: () => void;
  hasEdits?: boolean;
  saveLoading?: boolean;
}

interface PageDimensions {
  width_in: number;
  height_in: number;
  margin_top_in: number;
  margin_bottom_in: number;
  margin_left_in: number;
  margin_right_in: number;
}

export function Preview({ resumeId, apiUrl, previewUrl, editable = false, onTextEdit, onSave, hasEdits = false, saveLoading = false }: PreviewProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const editContainerRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);
  const [loadError, setLoadError] = useState(false);
  const [htmlContent, setHtmlContent] = useState<string>("");
  const containerRef = useRef<HTMLDivElement>(null);
  const [pageDimensions, setPageDimensions] = useState<PageDimensions>({
    width_in: 8.5,
    height_in: 11.0,
    margin_top_in: 0.5,
    margin_bottom_in: 0.5,
    margin_left_in: 0.5,
    margin_right_in: 0.5,
  });

  const src = previewUrl || `${apiUrl}/preview/${resumeId}`;

  // Fetch page dimensions on mount or when resumeId changes
  useEffect(() => {
    fetch(`${apiUrl}/page-dimensions/${resumeId}`)
      .then(res => res.json())
      .then(dims => setPageDimensions(dims))
      .catch(err => console.error("Failed to load page dimensions:", err));
  }, [resumeId, apiUrl]);

  useEffect(() => {
    setLoadError(false);
    // Fetch HTML content if editable
    if (editable && src) {
      fetch(src)
        .then(res => res.text())
        .then(html => setHtmlContent(html))
        .catch(err => {
          console.error("Failed to load preview HTML:", err);
          setLoadError(true);
        });
    }
  }, [src, editable]);

  // Set up Shadow DOM when content is loaded
  useEffect(() => {
    if (editable && htmlContent && editContainerRef.current) {
      try {
        // Always rebuild from scratch: an existing shadow root just had its
        // content wiped below but was never refilled on a second run of
        // this effect (e.g. htmlContent changing without a full remount),
        // leaving the preview blank. Reuse the shadow root itself but
        // always create a fresh wrapper for the latest htmlContent.
        const shadowRoot =
          editContainerRef.current.shadowRoot ??
          editContainerRef.current.attachShadow({ mode: "open" });
        shadowRoot.innerHTML = "";

        const wrapper = document.createElement("div");
        wrapper.contentEditable = "true";
        wrapper.style.cssText = "outline: none; cursor: text;";
        wrapper.innerHTML = htmlContent;
        shadowRoot.appendChild(wrapper);

        // Skills rows have their own dedicated Accept/Reject review flow.
        // Free-text edits typed here can't be parsed back into individual
        // skill records without risking silently corrupting the category,
        // so lock them out of the editable region entirely rather than
        // accepting edits that Save then quietly drops.
        wrapper.querySelectorAll<HTMLElement>("[data-skill-category]").forEach((el) => {
          el.contentEditable = "false";
        });

        // Set up mutation observer to track bullet edits
        if (onTextEdit) {
          const observer = new MutationObserver(() => {
            // Debounce to avoid too many updates
            clearTimeout((observer as any).debounceTimeout);
            (observer as any).debounceTimeout = setTimeout(() => {
              // Read the real bullet id the renderer stamped on each
              // bullet div — previously this fabricated a random id per
              // edit, which never matched a real bullet on save, so
              // nothing typed here was ever actually persisted.
              const bulletDivs = wrapper.querySelectorAll<HTMLElement>("[data-bullet-id]");
              bulletDivs.forEach((div) => {
                const bulletId = div.dataset.bulletId;
                const text = div.textContent?.trim();
                if (bulletId && text && text.startsWith("•")) {
                  const cleanText = text.substring(1).trim();
                  onTextEdit(bulletId, cleanText);
                }
              });
            }, 500);
          });

          observer.observe(wrapper, {
            childList: true,
            subtree: true,
            characterData: true,
          });

          return () => observer.disconnect();
        }
      } catch (err) {
        console.error("Failed to set up Shadow DOM:", err);
      }
    }
  }, [htmlContent, editable, onTextEdit]);

  useEffect(() => {
    const updateScale = () => {
      if (containerRef.current) {
        const containerWidth = containerRef.current.clientWidth - 48;
        const pageWidth = pageDimensions.width_in * 96;
        const newScale = Math.min(1, containerWidth / pageWidth);
        setScale(newScale);
      }
    };

    updateScale();
    window.addEventListener("resize", updateScale);
    return () => window.removeEventListener("resize", updateScale);
  }, [pageDimensions]);

  // Calculate pixel dimensions from page dimensions (96 DPI)
  const pageWidthPx = pageDimensions.width_in * 96;
  const pageHeightPx = pageDimensions.height_in * 96;

  return (
    <div ref={containerRef} className="flex flex-col items-center w-full">
      <div className="flex items-center justify-between w-full px-4 mb-3">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setScale((s) => Math.max(0.5, s - 0.1))}
            className="px-2 py-1 text-xs bg-white rounded border border-gray-300 hover:bg-gray-50"
          >
            -
          </button>
          <span className="text-xs text-gray-500 w-12 text-center">
            {Math.round(scale * 100)}%
          </span>
          <button
            onClick={() => setScale((s) => Math.min(1.5, s + 0.1))}
            className="px-2 py-1 text-xs bg-white rounded border border-gray-300 hover:bg-gray-50"
          >
            +
          </button>
          <button
            onClick={() => setScale(1)}
            className="px-2 py-1 text-xs bg-white rounded border border-gray-300 hover:bg-gray-50"
          >
            100%
          </button>
        </div>

        {onSave && (
          <button
            onClick={onSave}
            disabled={!hasEdits || saveLoading}
            className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
              hasEdits && !saveLoading
                ? "bg-blue-600 text-white hover:bg-blue-700"
                : "bg-gray-100 text-gray-400 cursor-not-allowed"
            }`}
          >
            {saveLoading ? "Saving..." : "Save Changes"}
          </button>
        )}
      </div>

      {loadError && (
        <div className="mb-2 px-3 py-2 bg-gray-50 text-gray-500 text-xs rounded">
          Preview failed to load. Try uploading your resume again.
        </div>
      )}

      <div
        style={{
          transform: `scale(${scale})`,
          transformOrigin: "top center",
          width: `${pageWidthPx}px`,
          minHeight: `${pageHeightPx}px`,
        }}
      >
        {editable && htmlContent ? (
          // Editable mode: Shadow DOM for isolated styling
          <div
            ref={editContainerRef}
            className="bg-white shadow-lg overflow-auto"
            style={{
              width: `${pageWidthPx}px`,
              minHeight: `${pageHeightPx}px`,
              display: "block",
            }}
          />
        ) : (
          // View mode: iframe
          <iframe
            ref={iframeRef}
            src={src}
            className="w-full border-0 bg-white shadow-lg"
            style={{
              width: `${pageWidthPx}px`,
              height: `${pageHeightPx}px`,
            }}
            title="Resume Preview"
            onError={() => setLoadError(true)}
          />
        )}
      </div>
    </div>
  );
}
