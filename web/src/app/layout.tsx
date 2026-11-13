import type { Metadata } from "next";
import { Inter, Noto_Sans_Devanagari } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const notoDevanagari = Noto_Sans_Devanagari({
  subsets: ["devanagari", "latin"],
  variable: "--font-devanagari",
  display: "swap",
});

export const metadata: Metadata = {
  title: "MCCIA Search",
  description: "Internal knowledge search for MCCIA staff",
  // There is no favicon file: an empty one stops the browser asking for /favicon.ico and logging a 404 on every visit.
  icons: { icon: "data:," },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body
        className={`${inter.variable} ${notoDevanagari.variable} bg-page font-sans leading-[1.6] text-ink antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
