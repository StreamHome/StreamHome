export function CinemaBackground() {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 0,
        pointerEvents: 'none',
        background: 'radial-gradient(ellipse at center, transparent 0%, #141414 70%)',
        backgroundColor: '#141414'
      }}
    />
  );
}
