"use client";

import { useCallback, useRef, useState } from "react";

interface UploadProps {
  onUpload: (file: File) => void;
  loading: boolean;
  error: string | null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function Upload({ onUpload, loading, error }: UploadProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [loadedFile, setLoadedFile] = useState<{ name: string; size: number } | null>(null);

  const handleFile = useCallback(
    (file: File) => {
      const ext = file.name.split(".").pop()?.toLowerCase();
      if (ext !== "docx") {
        return;
      }
      setLoadedFile({ name: file.name, size: file.size });
      onUpload(file);
    },
    [onUpload]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOver(false);
  }, []);

  return (
    <div className="p-4 border-b border-gray-200">
      <div
        onClick={() => fileInputRef.current?.click()}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        className={`border-2 border-dashed rounded-lg text-center cursor-pointer transition-colors ${
          dragOver
            ? "border-gray-400 bg-gray-100 p-6"
            : loadedFile && !loading
              ? "border-gray-300 bg-gray-50/50 p-3"
              : "border-gray-300 hover:border-gray-400 p-6"
        }`}
      >
        {loading ? (
          <div className="text-sm text-gray-500 py-3">
            <div className="animate-spin inline-block w-5 h-5 border-2 border-gray-200 rounded-full mb-2" style={{borderTopColor: 'var(--muted-blue)'}} />
            <div>Parsing resume...</div>
          </div>
        ) : loadedFile ? (
          <div className="flex items-center gap-3">
            {/* File icon */}
            <div className="w-9 h-9 rounded-lg bg-gray-100 flex items-center justify-center shrink-0">
              <svg className="w-5 h-5 text-gray-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
              </svg>
            </div>
            <div className="flex-1 text-left min-w-0">
              <div className="text-sm font-medium text-gray-800 truncate">{loadedFile.name}</div>
              <div className="text-xs text-gray-400">{formatBytes(loadedFile.size)} — click to replace</div>
            </div>
            {/* Green check */}
            <div className="w-5 h-5 rounded-full flex items-center justify-center shrink-0" style={{background: 'var(--muted-green)'}}>
              <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
            </div>
          </div>
        ) : (
          <>
            <div className="text-sm font-medium text-gray-700 mb-1">
              Upload Resume
            </div>
            <div className="text-xs text-gray-400">
              Drop a .docx file or click to browse
            </div>
          </>
        )}
      </div>

      {error && (
        <div className="mt-2 text-sm text-gray-500 bg-gray-50 rounded px-3 py-2">
          {error}
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept=".docx"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFile(file);
          e.target.value = "";
        }}
      />
    </div>
  );
}
