/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        // A person typing: a small bob. Used only while the desk is working.
        bob: { '0%,100%': { transform: 'translateY(0)' }, '50%': { transform: 'translateY(-2px)' } },
        glow: { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.55' } },
        shake: { '0%,100%': { transform: 'translateX(0)' }, '25%': { transform: 'translateX(-1.5px)' }, '75%': { transform: 'translateX(1.5px)' } },
      },
      // Used only behind `motion-safe:` so reduced-motion visitors get no animation.
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
        bob: 'bob 0.6s ease-in-out infinite',
        glow: 'glow 1.2s ease-in-out infinite',
        shake: 'shake 0.25s linear infinite',
      },
    },
  },
  plugins: [],
};
