import { useState, useRef, useCallback } from "react";
import { Volume2, Square } from "lucide-react";

export function useAudioSpeech() {
  const [speaking, setSpeaking] = useState(false);
  const [speakingId, setSpeakingId] = useState(null);

  const speak = useCallback((id, text) => {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.05;
    utterance.pitch = 1.0;
    utterance.volume = 1.0;

    const voices = window.speechSynthesis.getVoices();
    const preferred = voices.find(
      (v) =>
        v.lang.startsWith("en") &&
        (v.name.includes("Google") || v.name.includes("Samantha") || v.name.includes("Daniel"))
    );
    if (preferred) utterance.voice = preferred;

    utterance.onstart = () => { setSpeaking(true); setSpeakingId(id); };
    utterance.onend = () => { setSpeaking(false); setSpeakingId(null); };
    utterance.onerror = () => { setSpeaking(false); setSpeakingId(null); };

    window.speechSynthesis.speak(utterance);
  }, []);

  const stop = useCallback(() => {
    window.speechSynthesis?.cancel();
    setSpeaking(false);
    setSpeakingId(null);
  }, []);

  return { speak, stop, speaking, speakingId };
}

export default function AudioControls({ msgId, text, speaking, speakingId, onSpeak, onStop }) {
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;
  if (!supported) return null;

  const isThisOne = speakingId === msgId;

  if (speaking && isThisOne) {
    return (
      <button
        onClick={onStop}
        title="Stop speaking"
        className="flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono text-amber bg-amber/10 border border-amber/25 hover:bg-amber/15 transition-colors"
      >
        <Square size={9} className="fill-current" />
        stop
      </button>
    );
  }

  return (
    <button
      onClick={() => onSpeak(msgId, text)}
      title="Read aloud"
      className="flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-mono text-muted border border-transparent hover:border-border hover:text-text transition-colors"
    >
      <Volume2 size={11} />
      speak
    </button>
  );
}
