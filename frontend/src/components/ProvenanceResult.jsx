import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { API_BASE } from "../config/api";
import SimilarReposCard from "./SimilarReposCard";
import { buildArtifactLayerGroups, buildLanguageSummary } from "../utils/layerViewModel";

const LAYERS = [
  "frontend",
  "backend",
  "messaging",
  "cache",
  "data",
  "observability",
  "infra",
  "testing",
  "ai_ml",
  "language_runtime",
];

const human = (value) => String(value ?? "-").replace(/_/g, " ");
const percent = (value) => (value == null ? "" : `${(value * 100).toFixed(1)}%`);

const techSource = (value) => {
  const source = String(value || "unknown").toLowerCase();
  if (source === "github_linguist") return { key: "linguist", label: "github linguist" };
  if (source === "manifest_table") return { key: "manifest-table", label: "manifest" };
  if (source === "manifest_heuristic") return { key: "manifest-heuristic", label: "manifest" };
  if (source === "manifest_ai_inferred") return { key: "manifest-ai", label: "manifest + AI" };
  if (source.includes("ai")) return { key: "ai-inferred", label: "AI inferred" };
  if (source.startsWith("manifest") || source === "project_manifest") {
    return { key: "manifest-heuristic", label: human(source) };
  }
  return { key: "other", label: human(source) };
};

const techSourceMark = (source) => {
  if (source.key === "manifest-ai") return "AI + M";
  if (source.key.includes("ai")) return "AI";
  return "M";
};

function InlineTechFeedback({
  tech,
  roles,
  feedbackState,
  onCorrect,
  onWrong,
  onRoleCorrection,
  onLayerCorrection,
}) {
  const currentRole = tech.technology_role || "library";
  const currentLayer = tech.architectural_layer?.primary || "";
  const [role, setRole] = useState("");
  const [layer, setLayer] = useState("");
  const [otherRole, setOtherRole] = useState("");
  const [otherLayer, setOtherLayer] = useState("");
  const [view, setView] = useState("idle");
  const [correctionKind, setCorrectionKind] = useState("technology");
  const [reason, setReason] = useState("");
  const pending = view === "submitting" || feedbackState?.[tech.name] === "pending";

  useEffect(() => {
    setRole(roles.find((item) => item.id !== currentRole)?.id || "");
  }, [roles, currentRole]);

  useEffect(() => {
    setLayer(LAYERS.find((item) => item !== currentLayer) || "");
  }, [currentLayer]);

  async function confirmTechnology() {
    setView("submitting");
    const accepted = await onCorrect(tech.name, currentRole);
    setView(accepted ? "submitted" : "idle");
  }

  async function submitCorrection() {
    setView("submitting");
    let accepted = false;
    if (correctionKind === "technology") {
      accepted = await onWrong(tech.name, reason || "User marked mapped technology incorrect");
    }
    if (correctionKind === "role") {
      accepted = await onRoleCorrection(
        tech.name,
        currentRole,
        role === "__other__" ? otherRole.trim() : role,
        reason || "Inline result correction",
      );
    }
    if (correctionKind === "layer") {
      accepted = await onLayerCorrection(
        tech.name,
        currentLayer,
        layer === "__other__" ? otherLayer.trim() : layer,
        reason || "Inline result correction",
      );
    }
    setView(accepted ? "submitted" : "correcting");
  }

  const correctionReady =
    correctionKind === "technology" ||
    (correctionKind === "role" && role && (role !== "__other__" || otherRole.trim())) ||
    (correctionKind === "layer" && layer && (layer !== "__other__" || otherLayer.trim()));
  const approvedOverlay = Boolean(
    tech.technology_role_overlay ||
      tech.architectural_layer_overlay ||
      tech.detection_source === "maintainer_learned",
  );
  const source = techSource(tech.detection_source);

  return (
    <div className={`inline-tech state-${view} ${approvedOverlay ? "approved-overlay" : ""}`}>
      <div className="inline-tech-main">
        <span className="inline-tech-name">
          <i className={`tech-source-mark source-${source.key}`}>{techSourceMark(source)}</i>
          <span className="tech-display-name">{tech.name}</span>
          <small className={`tech-source-badge source-${source.key}`}>{source.label}</small>
          <small className="tech-role-badge">{human(currentRole)}</small>
          {approvedOverlay && (
            <small className="approved-chip">
              {tech.detection_source === "maintainer_learned" ? "learned" : "approved"}
            </small>
          )}
        </span>
        {tech.technology_role_pipeline && (
          <span className="inline-meta tech-role-origin">
            role was {tech.technology_role_pipeline}
          </span>
        )}
      </div>

      {view === "idle" && (
        <div className="inline-tech-votes">
          <button className="vote yes" onClick={confirmTechnology} aria-label={`${tech.name} is correct`}>
            <span aria-hidden="true">✓</span>
          </button>
          <button className="vote no" onClick={() => setView("correcting")} aria-label={`Correct ${tech.name}`}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
      )}

      {view === "correcting" && (
        <div className="inline-tech-correction">
          <div className="correction-choice">
            <label>
              <input
                type="radio"
                name={`correction-${tech.name}`}
                checked={correctionKind === "technology"}
                onChange={() => setCorrectionKind("technology")}
              />
              Technology is not actually used
            </label>
          </div>
          <div className="correction-choice">
            <label>
              <input
                type="radio"
                name={`correction-${tech.name}`}
                checked={correctionKind === "role"}
                onChange={() => setCorrectionKind("role")}
              />
              Technology used but wrong role
            </label>
            {correctionKind === "role" && (
              <>
                <select value={role} onChange={(event) => setRole(event.target.value)}>
                  {roles
                    .filter((item) => item.id !== currentRole)
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  <option value="__other__">Other - new role</option>
                </select>
                {role === "__other__" && (
                  <input
                    className="other-value"
                    value={otherRole}
                    onChange={(event) => setOtherRole(event.target.value)}
                    placeholder="Enter new technology role"
                  />
                )}
              </>
            )}
          </div>
          <div className="correction-choice">
            <label>
              <input
                type="radio"
                name={`correction-${tech.name}`}
                checked={correctionKind === "layer"}
                onChange={() => setCorrectionKind("layer")}
              />
              Wrong architectural layer
            </label>
            {correctionKind === "layer" && (
              <>
                <select value={layer} onChange={(event) => setLayer(event.target.value)}>
                  {LAYERS.filter((item) => item !== currentLayer).map((item) => (
                    <option key={item} value={item}>
                      {human(item)}
                    </option>
                  ))}
                  <option value="__other__">Other - new layer</option>
                </select>
                {layer === "__other__" && (
                  <input
                    className="other-value"
                    value={otherLayer}
                    onChange={(event) => setOtherLayer(event.target.value)}
                    placeholder="Enter new architectural layer"
                  />
                )}
              </>
            )}
          </div>
          <div className="inline-tech-correction-actions">
            <button className="vote no active" onClick={() => setView("idle")} aria-label="Cancel correction">
              ×
            </button>
            <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Optional reason" />
            <button className="submit" disabled={!correctionReady || pending} onClick={submitCorrection}>
              submit correction
            </button>
          </div>
        </div>
      )}

      {view === "submitting" && <span className="feedback-submitting">submitting feedback...</span>}
      {view === "submitted" && (
        <span className="feedback-submitted">
          <i>✓</i> Feedback submitted
        </span>
      )}
    </div>
  );
}

export default function ProvenanceResult({
  result,
  feedbackState,
  onSoftwareTypeFeedback,
  onTechCorrect,
  onTechWrong,
  onRoleCorrection,
  onLayerCorrection,
}) {
  const { repo = {}, stack = {}, repository_classification: classification = {} } = result;
  const { primary, otherLanguages } = buildLanguageSummary(stack);
  const groups = buildArtifactLayerGroups(stack, classification);
  const confidence = stack.software_type_confidence ?? 0;
  const topics = repo.topics ?? [];
  const [types, setTypes] = useState([]);
  const [roles, setRoles] = useState([]);
  const [correctedType, setCorrectedType] = useState("");
  const [typeFeedbackState, setTypeFeedbackState] = useState("idle");
  const artifactSignature = groups
    .map(({ artifact, layers }) => `${artifact.name}:${layers.map(({ layer, techs }) => `${layer}-${techs.length}`).join(",")}`)
    .join("|");
  const [expandedArtifacts, setExpandedArtifacts] = useState({});

  useEffect(() => {
    Promise.all([
      fetch(`${API_BASE}/api/taxonomy/software_types`).then((response) => response.json()),
      fetch(`${API_BASE}/api/taxonomy/technology_roles`).then((response) => response.json()),
    ])
      .then(([typeData, roleData]) => {
        const nextTypes = typeData.software_types || [];
        setTypes(nextTypes);
        setRoles(roleData.technology_roles || []);
        setCorrectedType(nextTypes.find((item) => item.id !== stack.software_type)?.id || "");
      })
      .catch(() => {});
  }, [stack.software_type]);

  useEffect(() => {
    setExpandedArtifacts((current) =>
      Object.fromEntries(
        groups.map(({ artifact }, index) => [
          artifact.name,
          current[artifact.name] ?? index === 0,
        ]),
      ),
    );
  }, [artifactSignature]);

  async function confirmSoftwareType() {
    setTypeFeedbackState("submitting");
    const accepted = await onSoftwareTypeFeedback(true);
    setTypeFeedbackState(accepted ? "submitted" : "idle");
  }

  async function submitSoftwareTypeCorrection() {
    if (!correctedType) return;
    setTypeFeedbackState("submitting");
    const accepted = await onSoftwareTypeFeedback(false, correctedType);
    setTypeFeedbackState(accepted ? "submitted" : "correcting");
  }

  const softwareTypeOverlay = result.software_type_overlay || stack.software_type_overlay;
  const displayedSoftwareType = softwareTypeOverlay?.corrected_value || stack.software_type || "unknown";
  const pipelineSoftwareType = softwareTypeOverlay?.pipeline_value || result.software_type?.pipeline;
  const hasSoftwareTypeOverlay = Boolean(softwareTypeOverlay?.corrected_value);
  const pipelineVersion = result.pipeline_version || stack.pipeline_version;
  const similarAnalysisId = result.repo_key || result.analysis_id || result.request_id;

  return (
    <div className="provenance-layout">
      <aside className="provenance-sidebar">
      <section className="result-card result-repo">
        <div className="result-repo-head">
          <h1>{repo.full_name || repo.name || "Repository"}</h1>
          <div>
            <span className="result-star">★ {(repo.stars ?? 0).toLocaleString()}</span>
            {repo.license && <span className="result-license">{repo.license}</span>}
          </div>
        </div>
        {repo.description && <p>{repo.description}</p>}
        <div className="result-topics">{topics.map((topic) => <span key={topic}>{topic}</span>)}</div>
      </section>

      {similarAnalysisId && <SimilarReposCard analysisId={similarAnalysisId} />}
      </aside>

      <main className="provenance-content">

      <section className={`result-card result-verdict ${typeFeedbackState === "submitted" ? "feedback-complete" : ""} ${hasSoftwareTypeOverlay ? "approved-type-overlay" : ""}`}>
        <div className="result-verdict-top">
          <div className="result-verdict-lead">
            <span><em>software_type</em> {displayedSoftwareType}</span>
            {hasSoftwareTypeOverlay && (
              <>
                <b className="software-type-approved">maintainer approved</b>
                {pipelineSoftwareType && pipelineSoftwareType !== displayedSoftwareType && (
                  <small className="software-type-was">was {human(pipelineSoftwareType)}</small>
                )}
              </>
            )}
            <b className="result-ai-chip">AI · {human(stack.software_type_ai || pipelineSoftwareType || stack.software_type)}</b>
            {stack.specific_identity && <b className="result-id-chip">specific_identity: {stack.specific_identity}</b>}
            {pipelineVersion && <b className="result-pipeline-chip">pipeline v{pipelineVersion}</b>}
          </div>
          <div className={`inline-type-feedback state-${typeFeedbackState}`}>
            {typeFeedbackState === "idle" && (
              <>
                <button className="vote yes" aria-label="Software type is correct" onClick={confirmSoftwareType}>✓</button>
                <button className="vote no" aria-label="Correct software type" onClick={() => setTypeFeedbackState("correcting")}>×</button>
              </>
            )}
            {typeFeedbackState === "correcting" && (
              <>
                <button className="vote no active" aria-label="Cancel correction" onClick={() => setTypeFeedbackState("idle")}>×</button>
                <select value={correctedType} onChange={(event) => setCorrectedType(event.target.value)}>
                  {types.filter((item) => item.id !== stack.software_type).map((item) => (
                    <option key={item.id} value={item.id}>{item.label}</option>
                  ))}
                </select>
                <button className="submit" disabled={!correctedType} onClick={submitSoftwareTypeCorrection}>submit correction</button>
              </>
            )}
            {typeFeedbackState === "submitting" && <span className="feedback-submitting">submitting feedback...</span>}
            {typeFeedbackState === "submitted" && <span className="feedback-submitted"><i>✓</i> Feedback submitted</span>}
          </div>
        </div>
        <div className="result-reason">
          <span>AI REASONING</span>
          <p>{stack.software_type_ai_reasoning || stack.software_type_reasoning || "No model reasoning was returned."}</p>
        </div>
        <div className="result-confidence">
          <span>confidence</span>
          <i><b style={{ width: `${confidence * 100}%` }} /></i>
          <strong>{confidence.toFixed(2)}</strong>
        </div>
      </section>

      <section className="result-card result-architecture-card">
        <div className="result-section-head">
          <div>
            <h2>Architecture layers</h2>
            <p>Entire stack grouped by each technology's primary architectural role.</p>
          </div>
          <span>{classification.artifact_count || "single"} artifact</span>
        </div>

        <div className="architecture-context-grid">
          <div className="architecture-language-card">
            <div className="result-language-labels">PRIMARY LANGUAGE OTHER LANGUAGES</div>
            <div className="result-languages">
              {primary && <span className="primary">{primary.name} <small>{percent(primary.byte_share)}</small></span>}
              {otherLanguages.map((lang) => <span key={lang.name}>{lang.name} <small>{percent(lang.byte_share)}</small></span>)}
            </div>
          </div>

        </div>

        {groups.map(({ artifact, layers }, index) => {
          const isExpanded = expandedArtifacts[artifact.name] ?? index === 0;
          const panelId = `artifact-${artifact.name.replace(/[^a-z0-9_-]/gi, "-")}`;
          const techCount = layers.reduce((count, { techs }) => count + techs.length, 0);

          return (
            <div className={`result-artifact ${isExpanded ? "expanded" : "collapsed"}`} key={artifact.name}>
              <button
                type="button"
                className="result-artifact-head artifact-toggle"
                aria-expanded={isExpanded}
                aria-controls={panelId}
                onClick={() =>
                  setExpandedArtifacts((current) => ({
                    ...current,
                    [artifact.name]: !(current[artifact.name] ?? index === 0),
                  }))
                }
              >
                {isExpanded ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
                <strong>{artifact.name}</strong>
                {artifact.primary && <b>PRIMARY</b>}
                <span>{human(artifact.type)} - {artifact.path || "/"}</span>
                <small>{techCount} tech{techCount === 1 ? "" : "s"}</small>
              </button>

              {isExpanded && (
                <div id={panelId} className="artifact-layers">
                  {layers.map(({ layer, techs: items }) => (
                    <div className="architecture-role-group" key={layer}>
                      <label>{human(layer)}</label>
                      <div className="result-deps architecture-feedback-grid">
                        {items.map((tech) => (
                          <InlineTechFeedback
                            key={`${layer}-${tech.name}`}
                            tech={tech}
                            roles={roles}
                            feedbackState={feedbackState}
                            onCorrect={onTechCorrect}
                            onWrong={onTechWrong}
                            onRoleCorrection={onRoleCorrection}
                            onLayerCorrection={onLayerCorrection}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {!groups.length && <p className="result-empty">No artifact-scoped architecture was returned.</p>}
      </section>

      </main>
    </div>
  );
}
