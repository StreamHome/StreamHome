import React from 'react';

export default function EmberHome({ tab, profileId }: { tab: string, profileId: string }) {
  return (
    <div className="w-full min-h-screen text-[var(--text-color)] font-sans pt-24 pb-12 px-12">
      {/* Top Glass Navbar */}
      <nav className="fixed top-0 left-0 right-0 h-20 glass-pane !border-t-0 !border-l-0 !border-r-0 !rounded-none z-50 flex items-center px-12 justify-between bg-black/50">
        <div className="text-2xl font-heading tracking-widest">STREAM<span className="text-[var(--accent-color)]">HOME</span></div>
        <div className="flex gap-8 font-mono text-sm tracking-widest">
          <a href={`/?profile=${profileId}&view=home`} className={tab === 'home' ? 'text-[var(--accent-color)]' : 'hover:text-white/80'}>HOME</a>
          <a href={`/?profile=${profileId}&view=movies`} className={tab === 'movies' ? 'text-[var(--accent-color)]' : 'hover:text-white/80'}>MOVIES</a>
          <a href={`/?profile=${profileId}&view=series`} className={tab === 'series' ? 'text-[var(--accent-color)]' : 'hover:text-white/80'}>SERIES</a>
          <a href={`/?profile=${profileId}&view=downloads`} className={tab === 'downloads' ? 'text-[var(--accent-color)]' : 'hover:text-white/80'}>DOWNLOADS</a>
        </div>
        <a href="/admin" className="w-10 h-10 border border-[var(--accent-color)] flex items-center justify-center font-mono cursor-pointer hover:bg-[var(--accent-color)] hover:text-black transition-colors">
          E
        </a>
      </nav>

      {/* Cinematic Hero Billboard */}
      <div className="w-full h-[65vh] glass-pane relative overflow-hidden mb-16 flex flex-col justify-end p-16 hover-glow group bg-gradient-to-tr from-gray-900 to-gray-800">
        <div className="absolute inset-0 bg-gradient-to-t from-black via-black/50 to-transparent z-10" />
        <div className="absolute inset-0 bg-[url('https://image.tmdb.org/t/p/original/8rpDcsfLJypbO6vtec8OQ3NuKc.jpg')] bg-cover bg-center opacity-40 group-hover:opacity-70 transition-opacity duration-700" />
        
        <div className="relative z-20 max-w-2xl">
          <h2 className="text-6xl font-heading mb-4 text-white drop-shadow-lg">FIGHT CLUB</h2>
          <p className="font-sans text-lg text-white/80 mb-8 line-clamp-3">A ticking-time-bomb insomniac and a slippery soap salesman channel primal male aggression into a shocking new form of therapy. Their concept catches on, with underground "fight clubs" forming in every town.</p>
          <div className="flex gap-4 font-mono">
            <button className="bg-[var(--accent-color)] text-black px-8 py-3 tracking-widest font-bold hover:bg-white transition-colors">WATCH NOW</button>
            <button className="glass-pane px-8 py-3 tracking-widest hover:border-[var(--accent-color)] hover:border-dashed transition-all">DETAILS</button>
          </div>
        </div>
      </div>

      {/* Holographic Tilt Cards */}
      <div>
        <h3 className="font-heading text-2xl mb-6 border-l-4 border-[var(--accent-color)] pl-4">RECENTLY ADDED</h3>
        <div className="grid grid-cols-5 gap-6">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="aspect-[2/3] glass-pane relative cursor-pointer hover-glow transition-all duration-300 transform hover:-translate-y-2 group overflow-hidden">
               <div className="absolute inset-0 bg-white/5 opacity-0 group-hover:opacity-100 transition-opacity z-10 flex items-center justify-center">
                 <div className="w-16 h-16 rounded-full border border-[var(--accent-color)] flex items-center justify-center bg-black/50 text-[var(--accent-color)]">▶</div>
               </div>
               <div className="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-black to-transparent z-20">
                 <p className="font-mono text-xs text-[var(--accent-color)] mb-1">MOVIE</p>
                 <h4 className="font-heading text-lg truncate">MEDIA TITLE {i}</h4>
               </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
