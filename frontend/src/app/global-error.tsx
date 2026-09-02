"use client"; // Error boundaries must be Client Components

// Catches an error thrown by the root layout itself (app/error.tsx does
// not wrap layout.tsx/template.tsx in the same segment, per Next's error
// boundary docs) -- unlikely given how little layout.tsx does, but this
// is the only way to avoid a raw framework error screen if it ever does.
// Must render its own <html>/<body> since it replaces the root layout.
export default function GlobalError({
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <html lang="en">
      <body style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "system-ui, sans-serif", background: "#f3f4f6" }}>
        <div style={{ textAlign: "center" }}>
          <h2>Something went wrong</h2>
          <button onClick={() => retry()} style={{ marginTop: "1rem", padding: "0.5rem 1rem", borderRadius: "0.375rem", background: "#2563eb", color: "white", border: "none", cursor: "pointer" }}>
            Try again
          </button>
        </div>
      </body>
    </html>
  );
}
