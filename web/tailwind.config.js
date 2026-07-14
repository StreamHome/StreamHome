/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        heading: ['var(--font-heading)'],
        mono: ['var(--font-mono)'],
        sans: ['var(--font-sans)'],
      },
      colors: {
        ember: {
          orange: '#FF5F1F',
          dark: '#8B310E',
          black: '#000000',
        },
        aurora: {
          black: '#050505',
          white: '#FFFFFF',
          gray: '#888888',
        },
        cinema: {
          bg: '#141414',
          red: '#E50914',
        },
        gemini: {
          bg: '#09090b',
          blue: '#4285F4',
          purple: '#9b72cb',
          red: '#d96570',
          amber: '#f4b400',
        }
      }
    },
  },
  plugins: [],
}
