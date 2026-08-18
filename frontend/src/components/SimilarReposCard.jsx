import { useEffect, useState } from "react";
import { Eye, Star } from "lucide-react";
import { API_BASE } from "../config/api";

function getNested(item, path, fallback = "") {
  return path.split(".").reduce((value, key) => value?.[key], item) ?? fallback;
}

function repoName(item) {
  const repoKey = item.repo_key || item.analysis_id || "";
  return getNested(item, "repo.full_name")
    || item.repo_name
    || item.full_name
    || repoKey.replace(/^[^:]+:/, "")
    || "unknown/repo";
}

function analysisTarget(item, name) {
  const id = item.repo_key || item.analysis_id || (name.includes("/") ? `github:${name}` : "");
  return id ? `/results/${encodeURIComponent(id)}` : null;
}

function software_type(item) {
  return getNested(item, "stack.software_type") || item.software_type || "unknown";
}

function stackPattern(item) {
  return getNested(item, "stack.stack_pattern") || item.stack_pattern || "Custom";
}

function whyThisStack(item) {
  return getNested(item, "stack.why_this_stack") || item.why_this_stack || item.software_type_reasoning || "";
}

function repoDescription(item) {
  return getNested(item, "repo.description") || item.description || whyThisStack(item);
}

function repoStars(item) {
  return Number(getNested(item, "repo.stars") || item.stars || item.stargazers_count || 0);
}

function primaryLanguage(item) {
  return getNested(item, "repo.language") || item.language || getNested(item, "stack.primary_language.name") || "";
}

function formatStars(value) {
  if (!value) return "0";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`;
  return value.toLocaleString();
}

function truncate(text, length = 80) {
  if (!text || text.length <= length) return text;
  return `${text.slice(0, length - 3)}...`;
}

export default function SimilarReposCard({ analysisId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [hidden, setHidden] = useState(false);

  useEffect(() => {
    if (!analysisId) return;
    let cancelled = false;
    setLoading(true);
    setHidden(false);

    fetch(`${API_BASE}/api/analyses/similar/${encodeURIComponent(analysisId)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Similar repos failed (${res.status})`);
        return res.json();
      })
      .then((payload) => {
        if (cancelled) return;
        if (!payload?.count) setHidden(true);
        setData(payload);
      })
      .catch(() => {
        if (!cancelled) setHidden(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [analysisId]);

  if (hidden) return null;

  const method = data?.method;
  const similar = data?.similar ?? [];
  const isIdentityMatch = method === "specific_identity_software_type";
  const visibleRepos = expanded ? similar : similar.slice(0, 3);

  if (!loading && similar.length === 0) return null;

  return (
    <section className="similar-repos-section">
      <div className="similar-repos-head">
        <h3>Similar Repositories</h3>
        {!loading && similar.length > 0 && (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="similar-repos-toggle"
          >
            {expanded ? "Collapse" : `${similar.length} found`}
          </button>
        )}
      </div>

      <div className="similar-repos-list">
        {loading &&
          Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="similar-repo-card is-loading">
              <div />
              <span />
            </div>
          ))}

        {!loading &&
          visibleRepos.map((item, index) => {
            const name = repoName(item);
            const score = Math.round((item.score ?? item.similarity ?? 0) * 100);
            const description = truncate(repoDescription(item), 86);
            const basis = item.match_basis?.replace(/\+/g, " + ").replace(/_/g, " ");
            const language = primaryLanguage(item);
            const target = analysisTarget(item, name);

            return (
              <article key={`${name}-${index}`} className="similar-repo-card">
                <div className="similar-repo-main">
                  {target ? (
                    <a href={target} target="_blank" rel="noopener noreferrer">{name}</a>
                  ) : (
                    <a href={`https://github.com/${name}`} target="_blank" rel="noopener noreferrer">
                      {name}
                    </a>
                  )}
                  {description && <p>{description}</p>}
                  <div className="similar-repo-meta">
                    <span><Star size={13} aria-hidden="true" /> {formatStars(repoStars(item))}</span>
                    {language && <span><i /> {language}</span>}
                    <small>{software_type(item).replace(/_/g, " ")}</small>
                  </div>
                  <div className="similar-repo-tags">
                    {basis && <small>{basis}</small>}
                    <small>{stackPattern(item)}</small>
                    {isIdentityMatch && <small>{Math.max(1, score)}% match</small>}
                  </div>
                </div>
                {target ? (
                  <a className="similar-repo-view" href={target} target="_blank" rel="noopener noreferrer">
                    <Eye size={13} aria-hidden="true" /> View
                  </a>
                ) : (
                  <a className="similar-repo-view" href={`https://github.com/${name}`} target="_blank" rel="noopener noreferrer">
                    <Eye size={13} aria-hidden="true" /> View
                  </a>
                )}
              </article>
            );
          })}
      </div>
    </section>
  );
}
