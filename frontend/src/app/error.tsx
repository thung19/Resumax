"use client"; // Error boundaries must be Client Components

import { useEffect } from "react";

// Found by a frontend audit: the app had no error.tsx anywhere and no
// ErrorBoundary/componentDidCatch in src/ at all, so any uncaught
// render-time exception (e.g. a malformed API response reaching an
// unguarded .filter() in TailoringReview.tsx) unmounted the entire React
// tree with no fallback UI -- a blank white page with no way to recover
// short of a manual refresh. This wraps the whole app (there's only one
// route segment) so a crash anywhere shows a recoverable message instead.
//
// retry() re-renders the segment's children fresh, which remounts
// page.tsx and its in-memory resumeId/tailoringResult state along with
// it -- there's currently no persistence for that state, so a retry does
// mean starting over, not resuming exactly where the crash happened.
export default function Error({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  useEffect(() => {
    console.error("Unhandled error in Resumax UI:", error);
  }, [error]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100 p-6">
      <div className="max-w-md w-full bg-white rounded-lg shadow p-6 text-center space-y-4">
        <h2 className="text-lg font-semibold text-gray-900">Something went wrong</h2>
        <p className="text-sm text-gray-600">
          Resumax hit an unexpected error and couldn&apos;t continue. Your current
          resume and job description upload will need to be re-added.
        </p>
        <button
          onClick={() => retry()}
          className="inline-flex items-center px-4 py-2 rounded-md bg-blue-600 text-white text-sm font-medium hover:bg-blue-700"
        >
          Try again
        </button>
      </div>
    </div>
  );
}
