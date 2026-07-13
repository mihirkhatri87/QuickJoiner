/** @type {import('tailwindcss').Config} */
// Colors resolve to CSS variables defined in src/index.css, which swap by
// data-theme (dark default + light). Restyling the whole app = editing those vars.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        panel: "var(--panel)",
        raised: "var(--raised)",
        raised2: "var(--raised-2)",
        fill: "var(--fill)",
        fill2: "var(--fill-2)",
        border: "var(--border)",
        hair: "var(--hair)",
        ink: "var(--text)",
        muted: "var(--muted)",
        faint: "var(--faint)",
        accent: {
          DEFAULT: "var(--accent)",
          hi: "var(--accent-hi)",
          ink: "var(--accent-ink)",
          soft: "var(--accent-soft)",
        },
        gold: {
          DEFAULT: "var(--gold)",
          hi: "var(--gold-hi)",
          soft: "var(--gold-soft)",
          line: "var(--gold-line)",
        },
        unknown: { DEFAULT: "var(--unknown)", soft: "var(--unknown-soft)" },
        danger: { DEFAULT: "var(--danger)", soft: "var(--danger-soft)" },
      },
      fontFamily: {
        display: ["var(--font-display)"],
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      borderRadius: {
        xs: "8px",
        sm: "11px",
        DEFAULT: "14px",
        lg: "18px",
      },
      boxShadow: {
        panel: "0 1px 0 0 var(--border) inset, 0 24px 60px -28px rgba(0,0,0,.65)",
        soft: "0 10px 30px -18px rgba(0,0,0,.55)",
        pop: "0 18px 44px -20px rgba(0,0,0,.6)",
      },
      keyframes: {
        pulse2: { "50%": { opacity: "0.6" } },
        slidein: { from: { transform: "translateX(103%)" }, to: { transform: "none" } },
        fadein: { from: { opacity: "0" }, to: { opacity: "1" } },
        rise: {
          from: { opacity: "0", transform: "translateY(14px)" },
          to: { opacity: "1", transform: "none" },
        },
        ping2: {
          "0%": { transform: "scale(1)", opacity: "0.6" },
          "100%": { transform: "scale(2.4)", opacity: "0" },
        },
      },
      animation: {
        pulse2: "pulse2 1.5s ease-in-out infinite",
        slidein: "slidein .24s cubic-bezier(.22,.85,.28,1)",
        fadein: "fadein .18s ease",
        rise: "rise .5s cubic-bezier(.22,.85,.28,1) both",
        ping2: "ping2 2.4s cubic-bezier(0,0,.2,1) infinite",
      },
    },
  },
  plugins: [],
};
