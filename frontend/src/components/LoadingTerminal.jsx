import { Terminal } from "lucide-react";

const LOGS = [
  ["INF", "Initializing StackSniffer analysis engine", "text-accent"],
  ["RUN", "Fetching repository...", "text-amber"],
  ["OK", "Repository metadata received", "text-green"],
  ["RUN", "Reading file tree...", "text-amber"],
  ["INF", "Manifest and source files indexed", "text-accent"],
  ["RUN", "Running pattern detection...", "text-amber"],
  ["DET", "Technology signals discovered", "text-[#ffddb8]"],
  ["RUN", "AI software_type classification...", "text-amber"],
  ["OK", "Analysis complete. Generating taxonomy.", "text-green"],
];

export default function LoadingTerminal({ steps = [], currentStep = 0 }) {
  const progress = Math.min(100, Math.round(((currentStep + 0.4) / Math.max(1, steps.length - 1)) * 100));
  const visibleCount = Math.min(LOGS.length, currentStep * 2 + 2);
  return <div className="relative z-10 flex w-full max-w-4xl flex-col">
    <div className="flex items-center justify-between rounded-t-lg border border-b-0 border-border bg-[#222a3d] px-4 py-2.5">
      <div className="flex items-center gap-3"><Terminal size={18} className="text-accent"/><span className="font-mono text-xs uppercase tracking-[.16em] text-muted">Analysis Engine</span></div>
      <div className="flex gap-2"><i className="h-3 w-3 rounded-full bg-border/60"/><i className="h-3 w-3 rounded-full bg-border/60"/><i className="h-3 w-3 rounded-full bg-border/60"/></div>
    </div>
    <div className="relative flex h-[400px] flex-col overflow-hidden rounded-b-lg border border-border bg-[#060e20] p-6 font-mono text-xs leading-loose shadow-2xl sm:text-sm">
      <div className="pointer-events-none absolute inset-0 z-10 bg-[linear-gradient(rgba(18,16,16,0)_50%,rgba(0,0,0,.22)_50%),linear-gradient(90deg,rgba(255,0,0,.035),rgba(0,255,0,.015),rgba(0,0,255,.035))] bg-[size:100%_2px,3px_100%]"/>
      <div className="relative z-20 mt-auto space-y-2 overflow-y-auto">
        {LOGS.slice(0, visibleCount).map(([tag, text, color], index) => { const active = index === visibleCount - 1; return <div key={`${tag}-${index}`} className="flex items-start gap-3"><span className={`${color} mt-1`}>&gt;</span><div><span className={color}>[{tag}]</span> <span className={active ? "text-text" : "text-muted"}>{text}</span>{active && <span className="ml-1 inline-block h-4 w-2 animate-pulse bg-green align-middle"/>}</div></div>; })}
      </div>
    </div>
    <div className="mx-auto mt-6 flex w-full max-w-md flex-col gap-2"><div className="flex justify-between font-mono text-[11px] text-muted"><span>{steps[currentStep] || "Processing..."}</span><span className="text-green">{progress}%</span></div><div className="h-1.5 overflow-hidden rounded-full border border-border/50 bg-[#222a3d]"><div className="relative h-full bg-green transition-all duration-500" style={{width:`${progress}%`}}><i className="absolute inset-0 animate-pulse bg-white/20"/></div></div></div>
  </div>;
}
