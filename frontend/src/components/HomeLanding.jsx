import TopNavigation from "./TopNavigation";

export default function HomeLanding({
  repoUrl,
  onChange,
  onSubmit,
  onDemo,
  inputRef,
  health,
  urlError,
  error,
  rateLimitCountdown,
}) {
  return (
    <div className="home-v2">
      <TopNavigation health={health} onNewAnalysis={() => inputRef.current?.focus()} />
      <main>
        <section className="home-hero">
          <div className="home-wrap">
            <div className="home-eyebrow">manifest-verified signals + model inference</div>
            <h1>
              Know what a repo <em>is</em> - and how sure we are.
            </h1>
            <p className="home-lede">
              Drop a GitHub URL. StackSniffer returns <code>software_type</code>,{" "}
              <code>technology_role</code>, and <code>architectural_layer</code> - and marks
              every signal by where it came from, so verified facts never get confused with a
              model&apos;s guess.
            </p>

            <form className={`home-analyze ${urlError ? "invalid" : ""}`} onSubmit={onSubmit}>
              <span>$&gt;</span>
              <input
                ref={inputRef}
                value={repoUrl}
                onChange={onChange}
                placeholder="github.com/owner/repo"
                aria-label="GitHub repository URL"
                aria-invalid={Boolean(urlError)}
                aria-describedby={urlError ? "repo-url-error" : undefined}
                spellCheck={false}
              />
              <button type="submit">Analyze</button>
            </form>

            {urlError && (
              <p id="repo-url-error" className="home-error">
                {urlError}
              </p>
            )}
            {error && !rateLimitCountdown && <p className="home-error">{error}</p>}
            {rateLimitCountdown != null && (
              <p className="home-error">GitHub rate limit hit. Retrying in {rateLimitCountdown}s...</p>
            )}

            <div className="home-subrow">
              <button onClick={onDemo}>&gt; try demo - bhaktivora9/stacksniffer</button>
              <span>no sign-in - no API key</span>
            </div>
          </div>
        </section>

        <section className="home-pipeline">
          <div className="home-wrap">
            <div className="home-section-label">How a signal earns its provenance</div>
            <div className="home-steps">
              <article>
                <div className="idx">
                  <i />
                  01 <span>ingest</span>
                </div>
                <h2>Clone &amp; parse</h2>
                <p>
                  Clone the repo and walk the file tree, skipping build artifacts and vendored
                  code. Nothing is classified yet.
                </p>
              </article>
              <article className="map">
                <div className="idx">
                  <i />
                  02 <span>map</span>
                </div>
                <h2>Verify from manifests</h2>
                <p>
                  Read lockfiles, dependency manifests, and config against known signatures.
                  What&apos;s proven here is tagged MAP and outranks anything inferred.
                </p>
              </article>
              <article className="ai">
                <div className="idx">
                  <i />
                  03 <span>ai</span>
                </div>
                <h2>Infer the rest</h2>
                <p>
                  Where manifests can&apos;t decide, Gemini classifies software type and role -
                  returned with a confidence score and marked AI, never as fact.
                </p>
              </article>
              <article className="review">
                <div className="idx">
                  <i />
                  04 <span>review</span>
                </div>
                <h2>Correct &amp; overlay</h2>
                <p>
                  Maintainers can correct any classification. Approved corrections apply as
                  provenance-bearing overlays on top of the model&apos;s output - the machine&apos;s
                  judgment is annotated, never erased.
                </p>
              </article>
            </div>
          </div>
        </section>
      </main>
      <footer>
        <div className="home-wrap">
          <span>StackSniffer - provenance-honest stack analysis</span>
          <span>MAP &gt; inferred - always</span>
        </div>
      </footer>
    </div>
  );
}
