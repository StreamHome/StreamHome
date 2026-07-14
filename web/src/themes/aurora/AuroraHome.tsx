import React from 'react';

export default function AuroraHome({ tab, profileId }: { tab: string, profileId: string }) {
  return (
    <div className="w-full min-h-screen text-white font-sans bg-[var(--bg-color)]">
      {/* Floating Pill Nav */}
      <nav className="fixed top-8 left-1/2 -translate-x-1/2 h-14 glass-pane !rounded-full z-50 flex items-center px-8 gap-8 shadow-2xl bg-white/5">
        <div className="text-xl font-heading font-black tracking-tighter">STREAMHOME</div>
        <div className="w-[1px] h-6 bg-white/20" />
        <div className="flex gap-6 font-mono text-xs uppercase tracking-widest">
          <a href={`/?profile=${profileId}&view=home`} className={`hover:text-white transition-colors ${tab === 'home' ? 'text-white font-bold' : 'text-gray-400'}`}>Home</a>
          <a href={`/?profile=${profileId}&view=movies`} className={`hover:text-white transition-colors ${tab === 'movies' ? 'text-white font-bold' : 'text-gray-400'}`}>Movies</a>
          <a href={`/?profile=${profileId}&view=series`} className={`hover:text-white transition-colors ${tab === 'series' ? 'text-white font-bold' : 'text-gray-400'}`}>Series</a>
        </div>
      </nav>

      {/* Spatial Liquid Hero */}
      <div className="w-full h-[80vh] relative overflow-hidden bg-gradient-to-tr from-gray-900 to-gray-800 mb-16">
        <div className="absolute inset-0 bg-[url('https://image.tmdb.org/t/p/original/8rpDcsfLJypbO6vtec8OQ3NuKc.jpg')] bg-cover bg-center opacity-60 mix-blend-screen scale-105 hover:scale-100 transition-transform duration-[2s]" />
        <div className="absolute inset-0 bg-gradient-to-r from-black/80 via-black/40 to-transparent z-10" />
        
        <div className="absolute bottom-20 left-20 z-20 max-w-xl">
          <h2 className="text-7xl font-extrabold mb-6 tracking-tighter">FIGHT CLUB</h2>
          <p className="text-white/70 mb-10 leading-relaxed font-light text-lg">A ticking-time-bomb insomniac and a slippery soap salesman channel primal male aggression into a shocking new form of therapy.</p>
          <button className="bg-white text-black px-8 py-4 rounded-full font-bold hover:scale-105 transition-transform">
            Watch Experience
          </button>
        </div>
      </div>

      <div className="px-20 pb-20">
        <div className="grid grid-cols-4 gap-8">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="aspect-[2/3] glass-pane !rounded-2xl hover-glow overflow-hidden relative group">
              <div className="absolute inset-0 bg-gradient-to-t from-black/80 via-transparent to-transparent z-10" />
              <div className="absolute bottom-6 left-6 right-6 z-20">
                <h4 className="font-bold text-xl mb-1 group-hover:-translate-y-2 transition-transform">Movie {i}</h4>
                <p className="text-white/50 text-sm opacity-0 group-hover:opacity-100 group-hover:-translate-y-2 transition-all">2024 • Action</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
