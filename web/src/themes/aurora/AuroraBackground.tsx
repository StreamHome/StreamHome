export function AuroraBackground() {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 0,
        overflow: 'hidden',
        background: '#050505',
      }}
    >
      <style>
        {`
          @keyframes blob-1 {
            0% { transform: translate(0, 0) scale(1); }
            33% { transform: translate(30vw, 20vh) scale(1.1); }
            66% { transform: translate(-20vw, 40vh) scale(0.9); }
            100% { transform: translate(0, 0) scale(1); }
          }
          @keyframes blob-2 {
            0% { transform: translate(0, 0) scale(1); }
            33% { transform: translate(-30vw, -20vh) scale(1.1); }
            66% { transform: translate(20vw, -40vh) scale(0.9); }
            100% { transform: translate(0, 0) scale(1); }
          }
          @keyframes blob-3 {
            0% { transform: translate(0, 0) scale(1); }
            33% { transform: translate(20vw, -30vh) scale(0.9); }
            66% { transform: translate(-10vw, 20vh) scale(1.1); }
            100% { transform: translate(0, 0) scale(1); }
          }
          @keyframes blob-4 {
            0% { transform: translate(0, 0) scale(1); }
            33% { transform: translate(-40vw, 10vh) scale(0.9); }
            66% { transform: translate(10vw, -20vh) scale(1.1); }
            100% { transform: translate(0, 0) scale(1); }
          }
          @keyframes blob-5 {
            0% { transform: translate(0, 0) scale(1); }
            33% { transform: translate(10vw, 40vh) scale(1.1); }
            66% { transform: translate(-30vw, -10vh) scale(0.9); }
            100% { transform: translate(0, 0) scale(1); }
          }
          .aurora-blob {
            position: absolute;
            border-radius: 50%;
            filter: blur(80px);
            mix-blend-mode: screen;
            opacity: 0.8;
          }
        `}
      </style>

      <div className="aurora-blob" style={{
        top: '10%', left: '20%', width: '40vw', height: '40vw',
        background: '#111', animation: 'blob-1 25s infinite ease-in-out'
      }} />
      <div className="aurora-blob" style={{
        top: '40%', right: '10%', width: '35vw', height: '35vw',
        background: '#222', animation: 'blob-2 30s infinite ease-in-out'
      }} />
      <div className="aurora-blob" style={{
        bottom: '10%', left: '30%', width: '45vw', height: '45vw',
        background: '#333', animation: 'blob-3 28s infinite ease-in-out'
      }} />
      <div className="aurora-blob" style={{
        top: '20%', right: '30%', width: '30vw', height: '30vw',
        background: '#1a1a1a', animation: 'blob-4 22s infinite ease-in-out'
      }} />
      <div className="aurora-blob" style={{
        bottom: '20%', right: '20%', width: '38vw', height: '38vw',
        background: '#0f0f0f', animation: 'blob-5 26s infinite ease-in-out'
      }} />
    </div>
  );
}
