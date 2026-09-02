import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/providers";
import { TopBar } from "@/components/shell/TopBar";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Tieout",
  description:
    "Multi-source reconciliation: sales register, PSP settlement report and bank statement, with a ground-truth-verifiable match rate and false-match rate.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <Providers>
          <a
            href="#main"
            className="sr-only rounded-md bg-surface px-3 py-2 text-xs focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus-visible:focus-ring"
          >
            Skip to content
          </a>
          <TopBar />
          {/* Pages own their own container. The run views need a full-bleed
              header rail that the page gutter would otherwise cut short. */}
          <main id="main" className="flex-1">
            {children}
          </main>
        </Providers>
      </body>
    </html>
  );
}
