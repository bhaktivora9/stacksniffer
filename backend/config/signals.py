"""
Centralized deterministic detection signals.

The detection pipeline uses these as one source of truth for Phase 0:
- filename signals (exact basenames or directory prefixes)
- extension signals

Keeping this in a dedicated module lets runtime and tests load the same data
without duplicating hardcoded tuples across services.
"""

# filename_or_directory -> (technology_name, technology_role, confidence)
FILE_SIGNALS: dict[str, tuple[str, str, float]] = {
    "Dockerfile":           ("Docker",         "infra",      1.0),
    "docker-compose.yml":   ("Docker",         "infra",      1.0),
    "docker-compose.yaml":  ("Docker",         "infra",      1.0),
    "Chart.yaml":           ("Kubernetes",     "infra",      1.0),
    "skaffold.yaml":        ("Skaffold",       "infra",      1.0),
    "go.mod":               ("Go",             "languages",  1.0),
    "Cargo.toml":           ("Rust",           "languages",  1.0),
    "Gemfile":              ("Ruby",           "languages",  0.99),
    "composer.json":        ("PHP",            "languages",  0.99),
    "pubspec.yaml":         ("Flutter",        "frameworks", 1.0),
    "mix.exs":              ("Elixir",         "languages",  1.0),
    "rebar.config":         ("Erlang",         "languages",  1.0),
    "manage.py":            ("Django",         "frameworks", 1.0),
    "angular.json":         ("Angular",        "frameworks", 1.0),
    "next.config.js":       ("Next.js",        "frameworks", 1.0),
    "next.config.ts":       ("Next.js",        "frameworks", 1.0),
    "nuxt.config.js":       ("Nuxt.js",        "frameworks", 1.0),
    "nuxt.config.ts":       ("Nuxt.js",        "frameworks", 1.0),
    "svelte.config.js":     ("SvelteKit",      "frameworks", 1.0),
    ".scalafmt.conf":       ("Scala",          "languages",  1.0),
    "build.sbt":            ("Scala",          "languages",  1.0),
    "mix.lock":             ("Elixir",         "languages",  0.95),
    ".github/workflows/":    ("GitHub Actions", "infra",      1.0),
}

# extension -> (technology_name, technology_role, confidence)
EXTENSION_SIGNALS: dict[str, tuple[str, str, float]] = {
    ".py":    ("Python",     "languages", 0.99),
    ".js":    ("JavaScript", "languages", 0.99),
    ".jsx":   ("JavaScript", "languages", 0.99),
    ".ts":    ("TypeScript", "languages", 0.99),
    ".tsx":   ("TypeScript", "languages", 0.99),
    ".go":    ("Go",         "languages", 0.99),
    ".rs":    ("Rust",       "languages", 0.99),
    ".java":  ("Java",       "languages", 0.99),
    ".kt":    ("Kotlin",     "languages", 0.99),
    ".kts":   ("Kotlin",     "languages", 0.95),
    ".swift": ("Swift",      "languages", 0.99),
    ".rb":    ("Ruby",       "languages", 0.99),
    ".c":     ("C",          "languages", 0.95),
    ".cpp":   ("C++",        "languages", 0.95),
    ".cc":    ("C++",        "languages", 0.95),
    ".cs":    ("C#",        "languages", 0.99),
    ".scala": ("Scala",      "languages", 0.99),
    ".ex":    ("Elixir",     "languages", 0.99),
    ".exs":   ("Elixir",     "languages", 0.95),
    ".hs":    ("Haskell",    "languages", 0.99),
    ".lua":   ("Lua",        "languages", 0.99),
    ".ml":    ("OCaml",      "languages", 0.99),
    ".dart":  ("Dart",       "languages", 0.99),
    ".zig":   ("Zig",        "languages", 0.99),
}

