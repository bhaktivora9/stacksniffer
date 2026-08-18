/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: "#0b0d0e",
        surface: "#111417",
        border: "#232a2f",
        accent: "#a3f600",
        green: "#a3f600",
        amber: "#e0a63c",
        "ai-purple": "#ad9bff",
        text: "#e6e9eb",
        muted: "#9aa6b0",
      },
      fontFamily: {
        mono: ["Geist Mono", "ui-monospace", "monospace"],
        sans: ["Geist", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
