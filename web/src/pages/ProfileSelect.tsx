import React, { useEffect, useState } from 'react';
import { useTheme } from '../contexts/ThemeContext';
import { Link, useNavigate } from 'react-router-dom';
import DriftingEmbers from '../components/DriftingEmbers';

export default function ProfileSelect() {
  const { theme, setTheme } = useTheme();
  const navigate = useNavigate();
  
  // Default to Ember on mount
  useEffect(() => {
    setTheme('ember');
  }, [setTheme]);

  const handleProfileSelect = (id: string, themeName: 'ember'|'aurora'|'cinema'|'gemini') => {
    setTheme(themeName);
    navigate(`/?profile=${id}&view=home`);
  };

  return (
    <div className="min-h-screen w-full flex flex-col items-center justify-center relative transition-colors duration-700 ease-in-out bg-[var(--bg-color)]">
      {theme === 'ember' && <DriftingEmbers />}
      
      <div className="z-10 text-center">
        <h1 className="text-5xl font-heading mb-16 text-[var(--text-color)] tracking-wide transition-colors duration-700">Select Identity</h1>
        
        <div className="flex gap-8">
          <div 
            onClick={() => handleProfileSelect('admin-1', 'ember')}
            onMouseEnter={() => setTheme('ember')}
            className={`w-48 h-72 glass-pane hover-glow flex flex-col items-center justify-center relative cursor-pointer transition-all duration-700 ${theme === 'ember' ? 'scale-105 border-[var(--accent-color)] border-dashed shadow-[0_0_15px_rgba(255,95,31,0.3)]' : 'scale-100'}`}
          >
            <span className="text-6xl font-heading font-light opacity-80 text-[var(--text-color)]">E</span>
            <span className="absolute bottom-6 font-mono text-sm tracking-widest text-[var(--text-color)]">EMBER</span>
            <div className="absolute top-4 left-4 flex items-center gap-2">
              <div className="w-2 h-2 rounded-full bg-[#FF5F1F] animate-pulse"></div>
              <span className="text-[10px] font-mono text-[#FF5F1F]">[ADMIN]</span>
            </div>
          </div>
          
          <div 
            onClick={() => handleProfileSelect('user-2', 'aurora')}
            onMouseEnter={() => setTheme('aurora')}
            className={`w-48 h-72 glass-pane hover-glow flex flex-col items-center justify-center relative cursor-pointer transition-all duration-700 ${theme === 'aurora' ? 'scale-105 bg-white/10 shadow-[0_15px_40px_rgba(255,255,255,0.1)]' : 'scale-100'}`}
          >
            <span className="text-6xl font-heading font-light opacity-80 text-[var(--text-color)]">A</span>
            <span className="absolute bottom-6 font-mono text-sm tracking-widest text-[var(--text-color)]">AURORA</span>
          </div>
          
          <div 
            onClick={() => handleProfileSelect('user-3', 'cinema')}
            onMouseEnter={() => setTheme('cinema')}
            className={`w-48 h-72 glass-pane hover-glow flex flex-col items-center justify-center relative cursor-pointer transition-all duration-700 ${theme === 'cinema' ? 'scale-105 shadow-[0_10px_30px_rgba(0,0,0,0.8)] z-10 bg-black/80' : 'scale-100'}`}
          >
            <span className="text-6xl font-heading font-light opacity-80 text-[var(--text-color)]">C</span>
            <span className="absolute bottom-6 font-mono text-sm tracking-widest text-[var(--text-color)]">CINEMA</span>
          </div>

          <div 
            onClick={() => handleProfileSelect('user-4', 'gemini')}
            onMouseEnter={() => setTheme('gemini')}
            className={`w-48 h-72 glass-pane hover-glow flex flex-col items-center justify-center relative cursor-pointer transition-all duration-700 ${theme === 'gemini' ? 'scale-105 shadow-[0_0_20px_rgba(66,133,244,0.2)]' : 'scale-100'}`}
            style={theme === 'gemini' ? {
              borderColor: 'transparent',
              backgroundClip: 'padding-box, border-box',
              backgroundOrigin: 'padding-box, border-box',
              backgroundImage: 'linear-gradient(to right, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.05)), linear-gradient(45deg, #4285F4, #9b72cb, #d96570, #f4b400)'
            } : {}}
          >
            <span className="text-6xl font-heading font-light opacity-80 text-[var(--text-color)]">G</span>
            <span className="absolute bottom-6 font-mono text-sm tracking-widest text-[var(--text-color)]">GEMINI</span>
          </div>
        </div>
      </div>
    </div>
  );
}
