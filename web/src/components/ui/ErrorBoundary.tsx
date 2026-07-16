import React from "react";

interface State {
  hasError: boolean;
}

export class ErrorBoundary extends React.Component<React.PropsWithChildren, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return (
        <main className="min-h-screen bg-[#0a0a0a] text-white grid place-items-center p-8">
          <div className="max-w-md text-center">
            <h1 className="text-2xl font-semibold">The web client could not render this screen.</h1>
            <p className="mt-3 text-white/60">Reload the page. If the problem continues, check the server connection.</p>
            <button className="interaction-button mt-6 rounded border border-white/30 px-5 py-3" onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}
