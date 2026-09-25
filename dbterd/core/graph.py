"""Deterministic edge normalization and subgraph analysis for ERD graphs.

Each algorithm adapter emits a flat list of :class:`~dbterd.core.models.Ref`
relationships. This module is the single place that turns that raw evidence
into a graph renderers can lay out safely:

* :func:`normalize_relationships` — duplicate relationships between the same
  pair of models collapse to one edge (bidirectional evidence included), self
  references are flagged and partitioned separately, and the surviving edge
  keeps the most complete evidence.
* :func:`analyze_subgraphs` — connected components over the non-self edges,
  with single-node components and directed cycles detected. Self references
  never participate in cycle detection.

The merge policy is documented on :class:`dbterd.core.models.RefSource`.
"""

import copy
from dataclasses import dataclass, field

from dbterd.core.models import Ref


# ---------------------------------------------------------------------------
# Evidence / merge key helpers
# ---------------------------------------------------------------------------


def _evidence_of(ref: Ref) -> list[str]:
    """Evidence ids carried by ``ref``, falling back to its own name.

    A raw relationship without explicit provenance is its own evidence (the
    test/constraint/node name), so evidence lists are never empty once edges
    reach the merge stage.
    """
    return list(ref.evidence) or [ref.name]


def evidence_score(ref: Ref) -> int:
    """Evidence completeness score.

    Counts distinct proof points (test names, constraint names, semantic
    entities), with a small bonus for the number of columns the edge pins
    down. Used to pick the most complete edge within a duplicate group;
    :class:`~dbterd.core.models.RefSource` priority only breaks ties.
    """
    return len(set(_evidence_of(ref))) * 10 + len(ref.column_map[0]) + len(ref.column_map[1])


def _endpoint_key(ref: Ref) -> frozenset[str]:
    """Unordered pair of endpoint names — identity of the *undirected* edge."""
    return frozenset(ref.table_map)


def _direction_key(ref: Ref) -> tuple[str, str]:
    """Ordered ``(parent/to, child/from)`` direction of the edge."""
    return (ref.table_map[0], ref.table_map[1])


def _merge_columns(base: Ref, other: Ref, flip: bool = False) -> None:
    """Append ``other``'s columns onto ``base`` in ``base``'s orientation.

    Args:
        base: Winning edge, mutated in place.
        other: Losing edge contributing its column evidence.
        flip: Whether ``other`` points the opposite way from ``base``.
    """
    other_to, other_from = (
        (other.column_map[1], other.column_map[0]) if flip else (other.column_map[0], other.column_map[1])
    )
    for existing, incoming in (
        (base.column_map[0], other_to),
        (base.column_map[1], other_from),
    ):
        known = {c.lower() for c in existing}
        for column in incoming:
            if column.lower() not in known:
                existing.append(column)
                known.add(column.lower())


def _merge_into(winner: Ref, loser: Ref, bidirectional: bool) -> Ref:
    """Fold ``loser``'s evidence into a copy of ``winner``.

    The winner keeps its structural fields (direction, cardinality, label and
    name); only provenance, columns, and the bidirectional flag accumulate.
    """
    merged = copy.deepcopy(winner)
    merged.evidence = list(dict.fromkeys([*_evidence_of(winner), *_evidence_of(loser)]))
    merged.is_self_reference = winner.is_self_reference
    merged.bidirectional = winner.bidirectional or bidirectional
    _merge_columns(merged, loser, flip=bidirectional)
    return merged


def _rank_key(ref: Ref, index: int) -> tuple[int, int, int]:
    """Ranking within a duplicate group: completeness, source priority, order.

    The negative index last makes the choice stable (first complete edge wins)
    when completeness and source priority tie.
    """
    return (evidence_score(ref), ref.source.priority, -index)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_relationships(relationships: list[Ref]) -> tuple[list[Ref], list[Ref]]:
    """Collapse duplicate relationships into one edge per model pair.

    Grouping is by the *unordered* endpoint pair, so:

    * repeated relationships in the same direction merge to one edge;
    * relationships discovered in both directions merge to one edge
      (``bidirectional=True``), never two crossing edges;
    * self references (both endpoints identical) are grouped on their own and
      returned separately with ``is_self_reference=True``.

    Within a group the edge with the most complete evidence wins structural
    fields; ties fall back to :class:`~dbterd.core.models.RefSource` priority,
    then first-seen order. All losing edges' evidence ids and columns are
    folded into the winner.

    Args:
        relationships: Raw, pre-normalization relationships (already mapped
            to entity names by ``make_up_relationships``).

    Returns:
        Tuple of ``(relationships, self_references)``. Each non-self model
        pair appears at most once in ``relationships``; self references are
        excluded from it.
    """
    groups: dict[frozenset[str], list[tuple[int, Ref]]] = {}
    self_groups: dict[str, list[tuple[int, Ref]]] = {}

    for index, ref in enumerate(relationships):
        table_to, table_from = ref.table_map[0], ref.table_map[1]
        if table_to == table_from:
            flagged = copy.deepcopy(ref)
            flagged.is_self_reference = True
            self_groups.setdefault(table_to, []).append((index, flagged))
        else:
            groups.setdefault(_endpoint_key(ref), []).append((index, ref))

    normalized: list[tuple[int, Ref]] = []
    for members in groups.values():
        directions = {_direction_key(ref) for _, ref in members}
        bidirectional = len(directions) > 1
        winner_index, winner = max(members, key=lambda item: _rank_key(item[1], item[0]))
        for index, member in members:
            if index == winner_index:
                continue
            flip = bidirectional and _direction_key(member) != _direction_key(winner)
            winner = _merge_into(winner, member, bidirectional=flip)
        winner.bidirectional = bidirectional
        if not winner.evidence:
            winner.evidence = [winner.name]
        normalized.append((min(i for i, _ in members), winner))

    self_references: list[tuple[int, Ref]] = []
    for members in self_groups.values():
        winner_index, winner = max(members, key=lambda item: _rank_key(item[1], item[0]))
        for index, member in members:
            if index != winner_index:
                winner = _merge_into(winner, member, bidirectional=False)
        winner.is_self_reference = True
        if not winner.evidence:
            winner.evidence = [winner.name]
        self_references.append((min(i for i, _ in members), winner))

    normalized.sort(key=lambda item: item[0])
    self_references.sort(key=lambda item: item[0])
    return [ref for _, ref in normalized], [ref for _, ref in self_references]


# ---------------------------------------------------------------------------
# Subgraph analysis
# ---------------------------------------------------------------------------


@dataclass
class Subgraph:
    """A connected component of the non-self-reference relationship graph.

    Attributes:
        name: Stable identifier, e.g. ``subgraph_1``.
        node_ids: Models belonging to the component, first-seen order.
        is_single_node: The component has one model and no relationships.
        has_cycle: A directed cycle exists inside the component. Self
            references never count.
        edge_ids: Evidence ids of the component's normalized edges.
    """

    name: str
    node_ids: list[str] = field(default_factory=list)
    is_single_node: bool = False
    has_cycle: bool = False
    edge_ids: list[str] = field(default_factory=list)


def _connected_components(nodes: list[str], edges: list[Ref]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Undirected connected components via union-find over edge endpoints.

    Returns the ``node -> root`` map and an insertion-ordered
    ``root -> members`` map (members are first-seen ordered, not sorted).
    """
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    for edge in edges:
        union(edge.table_map[0], edge.table_map[1])

    root_of: dict[str, str] = {}
    components: dict[str, list[str]] = {}
    for node in nodes:
        root = find(node)
        root_of[node] = root
        components.setdefault(root, []).append(node)
    return root_of, components


def _has_directed_cycle(nodes: set[str], edges: list[Ref]) -> bool:
    """True when the directed (FK -> parent) subgraph on ``nodes`` cycles.

    Iterative DFS with white/gray/black coloring. Self loops are absent by
    construction (self references are partitioned out before this runs).
    """
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for edge in edges:
        source_node, target_node = edge.table_map[1], edge.table_map[0]
        if source_node in nodes and target_node in nodes:
            adjacency[source_node].append(target_node)

    white, gray, black = 0, 1, 2
    color = dict.fromkeys(nodes, white)
    for start in nodes:
        if color[start] != white:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = gray
        while stack:
            node, next_neighbor = stack[-1]
            neighbors = adjacency[node]
            if next_neighbor < len(neighbors):
                stack[-1] = (node, next_neighbor + 1)
                neighbor = neighbors[next_neighbor]
                if color[neighbor] == gray:
                    return True
                if color[neighbor] == white:
                    color[neighbor] = gray
                    stack.append((neighbor, 0))
            else:
                color[node] = black
                stack.pop()
    return False


def analyze_subgraphs(tables: list[str], relationships: list[Ref]) -> list[Subgraph]:
    """Group models into connected subgraphs and flag singles and cycles.

    Args:
        tables: Every model/table name that should appear in the output,
            including isolated (relationship-free) models.
        relationships: Normalized non-self relationships.
    Returns:
        One :class:`Subgraph` per connected component, in first-seen table
        order. Isolated models arrive as ``is_single_node`` components; any
        component with a directed cycle gets ``has_cycle=True``.
    """
    nodes = list(dict.fromkeys(tables))
    edges = [edge for edge in relationships if not edge.is_self_reference]
    root_of, components = _connected_components(nodes, edges)

    subgraphs: list[Subgraph] = []
    for number, root in enumerate(components, start=1):
        members = components[root]
        member_set = set(members)
        component_edges = [
            edge for edge in edges if root_of.get(edge.table_map[0]) == root and root_of.get(edge.table_map[1]) == root
        ]
        subgraph = Subgraph(
            name=f"subgraph_{number}",
            node_ids=members,
            is_single_node=len(members) == 1 and not component_edges,
            has_cycle=len(members) > 1 and _has_directed_cycle(member_set, component_edges),
            edge_ids=list(dict.fromkeys(evidence for edge in component_edges for evidence in _evidence_of(edge))),
        )
        subgraphs.append(subgraph)

    return subgraphs
