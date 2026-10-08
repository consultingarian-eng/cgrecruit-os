/** @type {import('tailwindcss').Config} */
module.exports = {
    darkMode: ["class"],
    content: [
        "./src/**/*.{js,jsx,ts,tsx}",
        "./public/index.html"
    ],
    theme: {
        extend: {
            fontFamily: {
                heading: ['Outfit', 'system-ui', 'sans-serif'],
                sans: ['"IBM Plex Sans"', 'system-ui', 'sans-serif'],
                mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
            },
            borderRadius: {
                lg: 'var(--radius)',
                md: 'calc(var(--radius) - 2px)',
                sm: 'calc(var(--radius) - 4px)'
            },
            colors: {
                background: 'hsl(var(--background))',
                foreground: 'hsl(var(--foreground))',
                surface: {
                    DEFAULT: '#141519',
                    hover: '#1D1E24',
                    active: '#262830',
                },
                ink: {
                    DEFAULT: '#F8F9FA',
                    muted: '#9CA3AF',
                    dim: '#8A909C',
                },
                strokes: {
                    DEFAULT: '#2A2D35',
                    focus: '#4A4D59',
                },
                brand: {
                    success: '#10B981',
                    primary: '#8B5CF6',
                    pink: '#EC4899',
                    settings: '#8B5CF6',
                    warning: '#F59E0B',
                    danger: '#EF4444',
                },
                // Candidate-facing brand palette (Apply, portal, Retry…). Keep in
                // step with the --cube-* tokens in src/index.css.
                cube: {
                    ink: '#0a0610',
                    surface: '#160d24',
                    line: '#3a2258',
                    magenta: '#ec008c',
                    purple: '#8a2bc2',
                    blue: '#4353ff',
                    cyan: '#22d3ee',
                    cream: '#f7eefb',
                    lav: '#c3b3d9',
                    dim: '#93819f',
                },
                card: {
                    DEFAULT: 'hsl(var(--card))',
                    foreground: 'hsl(var(--card-foreground))'
                },
                popover: {
                    DEFAULT: 'hsl(var(--popover))',
                    foreground: 'hsl(var(--popover-foreground))'
                },
                primary: {
                    DEFAULT: 'hsl(var(--primary))',
                    foreground: 'hsl(var(--primary-foreground))'
                },
                secondary: {
                    DEFAULT: 'hsl(var(--secondary))',
                    foreground: 'hsl(var(--secondary-foreground))'
                },
                muted: {
                    DEFAULT: 'hsl(var(--muted))',
                    foreground: 'hsl(var(--muted-foreground))'
                },
                accent: {
                    DEFAULT: 'hsl(var(--accent))',
                    foreground: 'hsl(var(--accent-foreground))'
                },
                destructive: {
                    DEFAULT: 'hsl(var(--destructive))',
                    foreground: 'hsl(var(--destructive-foreground))'
                },
                border: 'hsl(var(--border))',
                input: 'hsl(var(--input))',
                ring: 'hsl(var(--ring))',
                chart: {
                    '1': 'hsl(var(--chart-1))',
                    '2': 'hsl(var(--chart-2))',
                    '3': 'hsl(var(--chart-3))',
                    '4': 'hsl(var(--chart-4))',
                    '5': 'hsl(var(--chart-5))'
                }
            },
            keyframes: {
                'accordion-down': { from: { height: '0' }, to: { height: 'var(--radix-accordion-content-height)' } },
                'accordion-up': { from: { height: 'var(--radix-accordion-content-height)' }, to: { height: '0' } },
                'fade-in': { from: { opacity: 0, transform: 'translateY(4px)' }, to: { opacity: 1, transform: 'translateY(0)' } },
            },
            animation: {
                'accordion-down': 'accordion-down 0.2s ease-out',
                'accordion-up': 'accordion-up 0.2s ease-out',
                'fade-in': 'fade-in 0.3s ease-out',
            }
        }
    },
    plugins: [require("tailwindcss-animate")],
};
