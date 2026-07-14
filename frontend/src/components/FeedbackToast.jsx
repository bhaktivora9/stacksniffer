import { useEffect } from "react";

const TYPE_CLASSES = {
  success: "border-green/30 bg-green/10 text-green",
  warn: "border-amber/30 bg-amber/10 text-amber",
  error: "border-red-400/30 bg-red-400/10 text-red-400",
};

export default function FeedbackToast({ message, type = "success", duration = 2000, onDone }) {
  useEffect(() => {
    const timer = window.setTimeout(() => onDone?.(), duration);
    return () => window.clearTimeout(timer);
  }, [duration, onDone]);

  return (
    <div
      className={`pointer-events-auto min-w-[260px] max-w-sm translate-x-0 rounded border px-3 py-2.5 text-sm font-sans shadow-lg transition-all duration-300 animate-toast-in ${
        TYPE_CLASSES[type] ?? TYPE_CLASSES.success
      }`}
      role="status"
    >
      {message}
    </div>
  );
}
