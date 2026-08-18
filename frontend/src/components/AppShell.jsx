import TopNavigation from "./TopNavigation";

export default function AppShell({ children, health, processingTime, actions, bare = false }) {
  if (bare) return <div className="result-shell min-h-screen">{children}</div>;
  return (
    <div className="app-shell min-h-screen bg-bg text-text">
      <TopNavigation health={health} actions={actions} />
      {children}
    </div>
  );
}
