"""Remote verification of benchmark manifest commits against GitHub.

Kept apart from benchmark_manifest so structural validation never needs the
network. Each frozen commit is resolved through the same resolver used for ad
hoc repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

try:
    from services.benchmark_manifest import BenchmarkManifest
    from services.repository_resolver import (
        RepositoryResolutionError,
        ResolvedRepository,
        resolve_repository_reference,
    )
except ModuleNotFoundError:
    from backend.services.benchmark_manifest import BenchmarkManifest
    from backend.services.repository_resolver import (
        RepositoryResolutionError,
        ResolvedRepository,
        resolve_repository_reference,
    )

Resolver = Callable[[str, str], Awaitable[ResolvedRepository]]


@dataclass(frozen=True)
class CommitVerification:
    repository_id: str
    frozen_commit: str
    resolved_commit: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.resolved_commit == self.frozen_commit


async def verify_manifest_commits(
    manifest: BenchmarkManifest,
    resolve: Resolver = resolve_repository_reference,
) -> list[CommitVerification]:
    """Check that every entry's frozen_commit exists at its URL; never raises per entry."""
    results = []
    for repo in manifest.repositories:
        try:
            resolved = await resolve(repo.url, repo.frozen_commit)
        except RepositoryResolutionError as exc:
            results.append(CommitVerification(repo.id, repo.frozen_commit, None, f"{type(exc).__name__}: {exc}"))
            continue
        error = None
        if resolved.commit_sha != repo.frozen_commit:
            error = f"resolved to {resolved.commit_sha}, not the declared commit"
        results.append(CommitVerification(repo.id, repo.frozen_commit, resolved.commit_sha, error))
    return results


def main(argv: list[str] | None = None) -> int:
    """Verify manifests against GitHub: python -m backend.services.manifest_verification [repositories.yaml ...]"""
    import argparse
    import asyncio

    try:
        from services.benchmark_manifest import ManifestValidationError, load_manifest
    except ModuleNotFoundError:
        from backend.services.benchmark_manifest import ManifestValidationError, load_manifest

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("paths", nargs="*", default=["repositories.yaml"])
    args = parser.parse_args(argv)

    failed = False
    for path in args.paths:
        try:
            manifest = load_manifest(path)
        except (ManifestValidationError, OSError) as exc:
            print(exc)
            failed = True
            continue
        print(f"{path}: dataset {manifest.dataset_version}, {manifest.fingerprint}")
        for result in asyncio.run(verify_manifest_commits(manifest)):
            status = "OK  " if result.ok else "FAIL"
            print(f"  {status} {result.repository_id} {result.frozen_commit}" + (f"  {result.error}" if result.error else ""))
            failed = failed or not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
