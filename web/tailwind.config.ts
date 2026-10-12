import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: "#1B3A6B", // deep navy (MCCIA brand)
        accent: "#E8871A", // warm orange
        page: "#F5F7FA", // page background
        surface: "#FFFFFF", // cards
        ink: "#1A1A2E", // body text
        muted: "#6B7280", // secondary text
      },
      fontFamily: {
        // Inter covers Latin; Noto Sans Devanagari takes over for Marathi/Hindi glyphs.
        sans: [
          "var(--font-inter)",
          "var(--font-devanagari)",
          "system-ui",
          "sans-serif",
        ],
      },
      // The typography plugin colours text gray by default; keep answers in brand navy.
      typography: ({ theme }: { theme: (path: string) => string }) => ({
        DEFAULT: {
          css: {
            "--tw-prose-body": theme("colors.primary"),
            "--tw-prose-headings": theme("colors.primary"),
            "--tw-prose-bold": theme("colors.primary"),
            "--tw-prose-links": theme("colors.primary"),
            "--tw-prose-bullets": theme("colors.primary"),
            "--tw-prose-counters": theme("colors.primary"),
            "--tw-prose-quotes": theme("colors.primary"),
          },
        },
      }),
    },
  },
  plugins: [require("@tailwindcss/typography")],
};

export default config;
