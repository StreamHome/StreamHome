import './ember/ember.css';
import './aurora/aurora.css';
import './cinema/cinema.css';
import './gemini/gemini.css';
import type { ThemeId, ThemeConfig } from '../types/theme';

export const THEME_CONFIGS: Record<ThemeId, ThemeConfig> = {
  ember: {
    id: 'ember',
    name: 'Ember',
    fonts: { headline: 'Playfair Display, serif', body: 'Inter, sans-serif', mono: 'JetBrains Mono, monospace' },
    colors: {
      background: '#1e100b',
      surface: '#2b1c17',
      glassFill: 'rgba(30, 16, 11, 0.4)',
      glassBorder: 'rgba(255, 95, 31, 0.15)',
      glassBorderHover: '#f97316',
      glassBlur: '12px',
      accent: '#ffb59c',
      accentGlow: 'rgba(255, 95, 31, 0.5)',
      textPrimary: '#ffffff',
      textSecondary: '#f9dcd4',
      textAccent: '#ffb59c',
      error: '#ffb4ab'
    },
    geometry: { borderRadius: '0.25rem' },
    animation: { duration: '500ms', easing: 'cubic-bezier(0.16, 1, 0.3, 1)' }
  },
  aurora: {
    id: 'aurora',
    name: 'Aurora',
    fonts: { headline: 'Inter, sans-serif', body: 'Inter, sans-serif', mono: 'JetBrains Mono, monospace' },
    colors: {
      background: '#050505',
      surface: '#0a0a0a',
      glassFill: 'rgba(255, 255, 255, 0.03)',
      glassBorder: 'rgba(255, 255, 255, 0.08)',
      glassBorderHover: 'rgba(255, 255, 255, 0.3)',
      glassBlur: '40px',
      accent: '#ffffff',
      accentGlow: 'rgba(255, 255, 255, 0.1)',
      textPrimary: '#ffffff',
      textSecondary: '#cccccc',
      textAccent: '#ffffff',
      error: '#ffb4ab'
    },
    geometry: { borderRadius: '16px' },
    animation: { duration: '400ms', easing: 'cubic-bezier(0.25, 1, 0.5, 1)' }
  },
  cinema: {
    id: 'cinema',
    name: 'Cinema',
    fonts: { headline: 'Bebas Neue, sans-serif', body: 'Montserrat, sans-serif', mono: 'Montserrat, sans-serif' },
    colors: {
      background: '#141414',
      surface: '#1f1f1f',
      glassFill: 'rgba(31, 31, 31, 0.95)',
      glassBorder: 'rgba(255, 255, 255, 0.08)',
      glassBorderHover: '#E50914',
      glassBlur: '0px',
      accent: '#E50914',
      accentGlow: 'rgba(0,0,0,0.25)',
      textPrimary: '#ffffff',
      textSecondary: '#b3b3b3',
      textAccent: '#E50914',
      error: '#ffb4ab'
    },
    geometry: { borderRadius: '6px' },
    animation: { duration: '300ms', easing: 'ease-in-out' }
  },
  gemini: {
    id: 'gemini',
    name: 'Gemini',
    fonts: { headline: 'Outfit, sans-serif', body: 'Outfit, sans-serif', mono: 'JetBrains Mono, monospace' },
    colors: {
      background: '#09090b',
      surface: '#111113',
      glassFill: 'rgba(255, 255, 255, 0.05)',
      glassBorder: 'rgba(255, 255, 255, 0.1)',
      glassBorderHover: 'transparent',
      glassBlur: '16px',
      accent: '#4285F4',
      accentGlow: 'rgba(66, 133, 244, 0.3)',
      textPrimary: '#ffffff',
      textSecondary: '#a1a1aa',
      textAccent: '#4285F4',
      error: '#ffb4ab'
    },
    geometry: { borderRadius: '12px' },
    animation: { duration: '500ms', easing: 'cubic-bezier(0.16, 1, 0.3, 1)' }
  }
};

export function getThemeConfig(id: ThemeId): ThemeConfig {
  return THEME_CONFIGS[id] || THEME_CONFIGS['ember'];
}
