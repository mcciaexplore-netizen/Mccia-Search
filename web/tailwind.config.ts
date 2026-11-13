import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Pure white page, charcoal text, MCCIA green for actions. No navy anywhere.
        primary: "#2E7D32", // the action colour (buttons, selected chips, focus rings): same green as `accent`
        accent: "#2E7D32", // MCCIA green: New chat, Send, the user's messages, the active chat, citations
        "accent-dark": "#256628", // green when hovered
        tint: "#F0F7F0", // light green tint: hover and selected backgrounds
        ink: "#1A1A1A", // charcoal: all text and headings
        muted: "#6B7280", // warm grey: subtitles, captions, timestamps
        line: "#E5E7EB", // the very subtle border
        sidebar: "#F5F5F5", // the sidebar, and the empty frames behind pictures
        page: "#FFFFFF", // the page
        surface: "#FFFFFF", // cards and the AI's messages
        warn: "#B45309", // amber: failures and alerts in Settings (green would read as success)
        // A small supporting palette, used sparingly next to the green: the blue of the MCCIA logo (a bright blue, not navy), a warm amber, a soft violet.
        azure: "#1E6FBF", // links, sources, small details
        "azure-tint": "#EAF2FB",
        sun: "#D97706", // warm highlights (borders, dots); text on amber uses `warn`, which is darker
        "sun-tint": "#FEF3E2",
        iris: "#6D5BD0",
        "iris-tint": "#EFEDFB",
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
        card: "0 1px 2px rgba(26, 26, 26, 0.05), 0 1px 3px rgba(26, 26, 26, 0.06)",
        "card-hover": "0 10px 24px rgba(46, 125, 50, 0.14), 0 2px 6px rgba(26, 26, 26, 0.06)", // a lifted card: a soft green glow
        "card-hover-azure": "0 10px 24px rgba(30, 111, 191, 0.16), 0 2px 6px rgba(26, 26, 26, 0.06)",
        "card-hover-sun": "0 10px 24px rgba(217, 119, 6, 0.16), 0 2px 6px rgba(26, 26, 26, 0.06)",
        "card-hover-iris": "0 10px 24px rgba(109, 91, 208, 0.16), 0 2px 6px rgba(26, 26, 26, 0.06)",
        float: "0 4px 20px rgba(26, 26, 26, 0.08), 0 1px 3px rgba(26, 26, 26, 0.06)", // the question bar
        "float-focus": "0 8px 28px rgba(46, 125, 50, 0.18), 0 2px 6px rgba(26, 26, 26, 0.06)",
      },
      // Only opacity, transform and scale move: all run on the GPU. prefers-reduced-motion turns all of it off (see globals.css).
      keyframes: {
        "message-in": {
          from: { opacity: "0", transform: "translateY(12px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.96)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
        "dot-bounce": {
          "0%, 70%, 100%": { transform: "translateY(0)" },
          "35%": { transform: "translateY(-6px)" },
        },
      },
      animation: {
        // "backwards", not "both": once it has finished, no transform is left on the message (a transform would trap fixed-position children).
        "message-in": "message-in 220ms ease-out backwards",
        "fade-in": "fade-in 200ms ease-out both",
        "scale-in": "scale-in 200ms ease-out both",
        "dot-bounce": "dot-bounce 1.1s ease-in-out infinite",
      },
      transitionDuration: {
        sidebar: "250ms",
      },
      // Every button gets press feedback (see globals.css: it scales down a little), so `scale` is part of the usual transitions, and the default curve is ease-out.
      transitionProperty: {
        DEFAULT: "color, background-color, border-color, text-decoration-color, fill, stroke, opacity, box-shadow, transform, scale, translate, filter",
        colors: "color, background-color, border-color, text-decoration-color, fill, stroke, scale",
      },
      transitionTimingFunction: {
        DEFAULT: "cubic-bezier(0, 0, 0.2, 1)",
      },
      // The typography plugin colours text gray by default; keep answers charcoal, with a clear heading hierarchy.
      typography: ({ theme }: { theme: (path: string) => string }) => ({
        DEFAULT: {
          css: {
            "--tw-prose-body": theme("colors.ink"),
            "--tw-prose-headings": theme("colors.ink"),
            "--tw-prose-bold": theme("colors.ink"),
            "--tw-prose-links": theme("colors.azure"),
            "--tw-prose-bullets": theme("colors.accent"),
            "--tw-prose-counters": theme("colors.ink"),
            "--tw-prose-quotes": theme("colors.ink"),
            lineHeight: "1.6",
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
