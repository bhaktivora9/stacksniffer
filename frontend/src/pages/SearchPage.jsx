import AppShell from "../components/AppShell";
import CorpusSearchPanel from "../components/CorpusSearchPanel";

export default function SearchPage() {
  return (
    <AppShell>
      <main className="min-h-[calc(100vh-3.5rem)] bg-bg px-4 py-6 md:px-8">
        <div className="mx-auto max-w-6xl">
          <div className="mb-6">
            <div className="font-mono text-[10px] uppercase tracking-[.18em] text-green">
              Corpus / Discovery
            </div>
            <h1 className="mt-1 text-3xl font-semibold tracking-tight text-[#d8e2ff]">
              Search learned stacks
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-muted">
              Find analyzed repositories by technology combination, software type, or technology role.
            </p>
          </div>
          <CorpusSearchPanel />
        </div>
      </main>
    </AppShell>
  );
}
