import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: "#0B1F3A", // navy (MCCIA brand): headings, the user's messages, the main text colour of answers
        accent: "#2E7D32", // green: the main actions (New chat, Send), the active chat, citations
        "accent-dark": "#256628", // accent when hovered or pressed
        warn: "#B45309", // amber: failures and alerts in Settings (green would read as success)
        page: "#F8F9FA", // page background (very light cool grey)
        surface: "#FFFFFF", // cards and the AI's messages
        ink: "#1A1A2E", // body text
        muted: "#5F6B7A", // secondary text (5.2:1 on white)
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
      boxShadow: {
        // The floating question bar, resting and focused; the photo hover; the glass panels.
        float: "0 8px 30px rgba(11, 31, 58, 0.10), 0 1px 3px rgba(11, 31, 58, 0.06)",
        "float-focus": "0 12px 36px rgba(46, 125, 50, 0.16), 0 2px 6px rgba(11, 31, 58, 0.08)",
        lift: "0 10px 24px rgba(11, 31, 58, 0.16)",
        glass: "0 8px 32px rgba(11, 31, 58, 0.08)",
      },
      // Only opacity and transform move: both run on the GPU. prefers-reduced-motion turns all of it off (see globals.css).
      keyframes: {
        "message-in": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.96)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        "pulse-dot": {
          "0%, 80%, 100%": { opacity: "0.3", transform: "scale(0.8)" },
          "40%": { opacity: "1", transform: "scale(1)" },
        },
      },
      animation: {
        // "backwards", not "both": once it has finished, no transform is left on the message (a transform would trap fixed-position children such as the photo viewer).
        "message-in": "message-in 200ms ease-out backwards",
        "fade-in": "fade-in 250ms ease-out both",
        "scale-in": "scale-in 250ms ease-out both",
        "pulse-dot": "pulse-dot 1.2s ease-in-out infinite",
      },
      transitionDuration: {
        sidebar: "280ms",
      },
      // The typography plugin colours text gray by default; keep answers in brand navy, with a clear heading hierarchy.
      typography: ({ theme }: { theme: (path: string) => string }) => ({
        DEFAULT: {
          css: {
            "--tw-prose-body": theme("colors.ink"),
            "--tw-prose-headings": theme("colors.primary"),
            "--tw-prose-bold": theme("colors.primary"),
            "--tw-prose-links": theme("colors.accent"),
            "--tw-prose-bullets": theme("colors.accent"),
            "--tw-prose-counters": theme("colors.primary"),
            "--tw-prose-quotes": theme("colors.primary"),
            lineHeight: "1.7",
            "h1, h2, h3, h4": { fontWeight: "600", letterSpacing: "-0.01em" },
            h1: { fontSize: "1.375em", marginTop: "1.2em", marginBottom: "0.5em" },
            h2: { fontSize: "1.2em", marginTop: "1.2em", marginBottom: "0.45em" },
            h3: { fontSize: "1.05em", marginTop: "1.1em", marginBottom: "0.4em" },
            "p, ul, ol": { marginTop: "0.75em", marginBottom: "0.75em" },
            "code::before": { content: '""' },
            "code::after": { content: '""' },
            "> :first-child": { marginTop: "0" },
            "> :last-child": { marginBottom: "0" },
          },
        },
      }),
    },
  },
  plugins: [require("@tailwindcss/typography")],
};

export default config;
