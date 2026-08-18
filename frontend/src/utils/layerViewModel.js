export const LAYER_ORDER = [
  "language_runtime",
  "frontend",
  "backend",
  "messaging",
  "cache",
  "data",
  "ai_ml",
  "observability",
  "infra",
  "testing",
  "unassigned",
];

export function buildLanguageSummary(stack) {
  const languages = Array.isArray(stack?.languages) ? stack.languages : [];
  const configuredPrimary = stack?.primary_language?.trim();
  const primary = configuredPrimary
    ? languages.find(
        (language) => language?.name?.toLowerCase() === configuredPrimary.toLowerCase()
      ) ?? { name: configuredPrimary }
    : languages.find((language) => language?.is_primary) ?? languages[0] ?? null;

  const primaryName = primary?.name?.toLowerCase();
  const otherLanguages = languages.filter(
    (language) => language?.name && language.name.toLowerCase() !== primaryName
  );

  return { primary, otherLanguages };
}

export function buildArtifactLayerGroups(stack, classification) {
  const configuredArtifacts = classification?.artifacts ?? [];
  const artifacts = configuredArtifacts.length ? configuredArtifacts : [{ name: "Repository", type: "repository", path: "/", primary: true }];
  const layeredTechs = Object.values(stack ?? {})
    .filter(Array.isArray)
    .flat()
    .filter((tech) => tech?.name && tech?.detection_source !== "github_linguist")
    .map((tech) => ({
      ...tech,
      architectural_layer: {
        ...(tech.architectural_layer ?? {}),
        primary: tech.architectural_layer?.primary || "unassigned",
      },
    }));

  if (!layeredTechs.length) return [];

  return [
    ...artifacts.map((artifact) => ({
      artifact,
      techs: configuredArtifacts.length
        ? layeredTechs.filter((tech) => tech.belongs_to_artifact === artifact.name)
        : layeredTechs,
    })),
    {
      artifact: {
        name: "Unassigned",
        type: "ambiguous ownership",
        path: null,
        primary: false,
      },
      techs: layeredTechs.filter((tech) => configuredArtifacts.length && !tech.belongs_to_artifact),
    },
  ]
    .filter((group) => group.techs.length > 0)
    .map(({ artifact, techs }) => {
      const unique = Array.from(
        new Map(
          techs.map((tech) => [
            `${tech.name.toLowerCase()}:${tech.architectural_layer.primary}`,
            tech,
          ])
        ).values()
      );
      const customLayers = [...new Set(unique.map((tech) => tech.architectural_layer.primary))]
        .filter((layer) => !LAYER_ORDER.includes(layer))
        .sort();
      return {
        artifact,
        layers: [...LAYER_ORDER, ...customLayers]
          .map((layer) => ({
            layer,
            techs: unique.filter(
              (tech) => tech.architectural_layer.primary === layer
            ),
          }))
          .filter((group) => group.techs.length > 0),
      };
    });
}
