const log = [];

export function record(kind, detail) {
  log.push({ kind, detail, at: Date.now() });
}
