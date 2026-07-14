import { useEffect, useRef } from "react";

export default function LoadingTerminal({ steps = [], currentStep = 0 }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [currentStep]);

  return (
    <div className="bg-bg border border-border rounded-lg overflow-hidden w-full max-w-xl">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-border bg-surface">
        <span className="w-3 h-3 rounded-full bg-[#ff5f57]" />
        <span className="w-3 h-3 rounded-full bg-[#febc2e]" />
        <span className="w-3 h-3 rounded-full bg-[#28c840]" />
        <span className="ml-2 font-mono text-xs text-muted">stacksniffer — analysis</span>
      </div>
      <div className="px-5 py-4 space-y-1.5 min-h-[160px]">
        {steps.map((step, i) => {
          const isDone = i < currentStep;
          const isCurrent = i === currentStep;
          const isPending = i > currentStep;

          return (
            <div key={i} className="flex items-center gap-3 font-mono text-sm">
              {isDone && (
                <>
                  <span className="text-green w-4 text-center shrink-0">✓</span>
                  <span className="text-green">{step}</span>
                </>
              )}
              {isCurrent && (
                <>
                  <span className="text-amber w-4 text-center shrink-0">→</span>
                  <span className="text-amber blink-cursor">{step}</span>
                </>
              )}
              {isPending && (
                <>
                  <span className="text-muted w-4 text-center shrink-0">○</span>
                  <span className="text-muted">{step}</span>
                </>
              )}
            </div>
          );
        })}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
