/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0a0e14', panel: '#0f141c', panel2: '#141b25', line: '#1f2a37', muted: '#7d8a9c', text: '#d6dde6',
        pos: '#22c55e', neg: '#ef4444', warn: '#f59e0b', info: '#3b82f6',
      },
      fontFamily: { sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'], mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'] },
    },
  },
  plugins: [],
}
