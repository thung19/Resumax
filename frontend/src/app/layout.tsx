import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Resumax",
  description: "Resume tailoring with formatting fidelity",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-gray-100">
        {children}
      </body>
    </html>
  );
}
