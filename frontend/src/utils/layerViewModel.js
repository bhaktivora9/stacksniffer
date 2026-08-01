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
];

export function buildArtifactLayerGroups(stack, classification) {
  const artifacts = classification?.artifacts ?? [];
  const layeredTechs = Object.values(stack ?? {})
    .filter(Array.isArray)
    .flat()
    .filter((tech) => tech?.name && tech?.architectural_layer?.primary);

  if (!artifacts.length || !layeredTechs.length) return [];

  return [
    ...artifacts.map((artifact) => ({
      artifact,
      techs: layeredTechs.filter(
        (tech) => tech.belongs_to_artifact === artifact.name
      ),
    })),
    {
      artifact: {
        name: "Unassigned",
        type: "ambiguous ownership",
        path: null,
        primary: false,
      },
      techs: layeredTechs.filter((tech) => !tech.belongs_to_artifact),
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
      return {
        artifact,
        layers: LAYER_ORDER
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

