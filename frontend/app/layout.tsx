import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Archive — AI Search",
  description: "Local AI-driven hybrid search over your document archive",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
