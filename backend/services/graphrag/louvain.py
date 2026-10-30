"""Deterministic Louvain community detection (weighted, undirected), in plain Python.

Blondel et al. (2008): repeatedly move single nodes to the neighbouring community with the best
modularity gain, then aggregate communities into nodes, until nothing moves. Node visiting order
comes from a seeded shuffle of the sorted node keys and ties go to the smallest community label,
so the same graph, seed and resolution always give the same partition. Graphs here are one
repository's projection (hundreds to a few thousand nodes), well within pure-Python reach.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Hashable, Iterable

ALGORITHM = "louvain"
ALGORITHM_VERSION = "stacksniffer-louvain/1"
MAX_PASSES = 100


def louvain(nodes: Iterable[Hashable], edges: Iterable[tuple[Hashable, Hashable, float]], *, seed: int = 42,
            resolution: float = 1.0) -> list[dict[Hashable, int]]:
    """Partitions of the original nodes, one per level, finest first. Labels are 0..n-1 per level."""
    order = sorted(set(nodes), key=repr)
    index = {node: i for i, node in enumerate(order)}
    graph: list[dict[int, float]] = [defaultdict(float) for _ in order]
    for a, b, weight in edges:
        if a in index and b in index and weight > 0:
            i, j = index[a], index[b]
            graph[i][j] += weight
            if i != j:
                graph[j][i] += weight
    rng = random.Random(seed)
    membership = list(range(len(order)))  # original node -> current aggregated node
    levels: list[dict[Hashable, int]] = []
    while True:
        partition, moved = _one_level(graph, rng, resolution)
        if not moved:
            break
        labels = {c: i for i, c in enumerate(sorted(set(partition)))}
        partition = [labels[c] for c in partition]
        membership = [partition[m] for m in membership]
        levels.append({node: membership[i] for i, node in enumerate(order)})
        graph = _aggregate(graph, partition, len(labels))
    if not levels:
        levels.append({node: i for i, node in enumerate(order)})
    return levels


def _one_level(graph: list[dict[int, float]], rng: random.Random, resolution: float) -> tuple[list[int], bool]:
    n = len(graph)
    degree = [sum(w * (2 if j == i else 1) for j, w in graph[i].items()) for i in range(n)]
    total = sum(degree) / 2
    community = list(range(n))
    if total == 0:
        return community, False
    tot = degree[:]
    visit = list(range(n))
    rng.shuffle(visit)
    moved_any = False
    for _ in range(MAX_PASSES):
        moved = False
        for i in visit:
            current = community[i]
            links: dict[int, float] = defaultdict(float)
            for j, w in graph[i].items():
                if j != i:
                    links[community[j]] += w
            tot[current] -= degree[i]
            best, best_gain = current, links.get(current, 0.0) - resolution * tot[current] * degree[i] / (2 * total)
            for c in sorted(links):
                gain = links[c] - resolution * tot[c] * degree[i] / (2 * total)
                if gain > best_gain + 1e-12:
                    best, best_gain = c, gain
            tot[best] += degree[i]
            if best != current:
                community[i] = best
                moved = moved_any = True
        if not moved:
            break
    return community, moved_any


def _aggregate(graph: list[dict[int, float]], partition: list[int], size: int) -> list[dict[int, float]]:
    merged: list[dict[int, float]] = [defaultdict(float) for _ in range(size)]
    for i, neighbours in enumerate(graph):
        for j, w in neighbours.items():
            a, b = partition[i], partition[j]
            if i == j or a != b:
                merged[a][b] += w
            elif i < j:  # an internal edge between two distinct nodes becomes one self-loop weight
                merged[a][a] += w
    return merged


def modularity(edges: Iterable[tuple[Hashable, Hashable, float]], partition: dict[Hashable, int]) -> float:
    degree: dict[Hashable, float] = defaultdict(float)
    internal: dict[int, float] = defaultdict(float)
    total = 0.0
    for a, b, w in edges:
        if a not in partition or b not in partition:
            continue
        total += w
        degree[a] += w
        degree[b] += w
        if partition[a] == partition[b]:
            internal[partition[a]] += w
    if total == 0:
        return 0.0
    by_community: dict[int, float] = defaultdict(float)
    for node, d in degree.items():
        by_community[partition[node]] += d
    return sum(internal[c] / total - (by_community[c] / (2 * total)) ** 2 for c in by_community)
