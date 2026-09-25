"""Relationship post-processing: dedupe, self-references, cycles, subgraphs.

This module hardens how parsed relationships are turned into diagram edges:

- **Duplicate merging** (:func:`merge_duplicate_refs`): relationships between
  the same *unordered* pair of tables are merged into a single edge, so a
  bidirectional pair no longer renders as two edges. The survivor is the ref
  with the most complete evidence (see :func:`evidence_score`).
- **Self-references** (:func:`split_self_references`): recursive relationships
  (``table_map[0] == table_map[1]``) are marked via ``Ref.is_self_reference``
  and excluded from cycle detection — a self-loop is not a cycle for layout
  purposes.
- **Cycle detection** (:func:`has_cycle` / :func:`find_cycle`): directed cycle
  detection over non-self relationships.
- **Subgraph grouping** (:func:`group_subgraphs`): connected components of the
  relationship graph, including singleton (single-table) components, each
  flagged with ``has_cycle``.

Relationship source priority
----------------------------
Relationships can originate from different dbt features. When the same table
pair is reported by more than one source, the more authoritative source wins
(see :data:`SOURCE_PRIORITY`):

1. ``foreign_key`` — dbt model contract ``foreign_key`` constraints. Enforced
   (or at least validated) by the warehouse, hence strongest evidence.
2. ``test`` — dbt relationship tests. Verified by ``dbt test``, but only when
   the test suite actually runs.
3. ``custom`` — declared mappings (semantic entities, user-defined metadata).
   Never verified; weakest evidence.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

from dbterd.core.models import Ref, Table


class RefSource(str, Enum):
    """Origin of a parsed relationship, in descending order of authority."""

    FOREIGN_KEY = "foreign_key"  # dbt model contract constraints (warehouse-enforced)
    TEST = "test"  # dbt relationship tests (verified by `dbt test`)
    CUSTOM = "custom"  # declared mappings: semantic entities, custom metadata


# Lower number = higher priority. Unknown sources sort last.
SOURCE_PRIORITY: dict[str, int] = {
    RefSource.FOREIGN_KEY.value: 0,
    RefSource.TEST.value: 1,
    RefSource.CUSTOM.value: 2,
}

_UNKNOWN_COLUMNS = {"", "unknown"}


def _columns_known(columns: list[str]) -> bool:
    """Whether a column list carries real column names (not placeholders)."""
    return any(str(col).strip('"').lower() not in _UNKNOWN_COLUMNS for col in columns)


def evidence_score(ref: Ref) -> tuple[int, int, int, int]:
    """Score how complete a relationship's evidence is; higher wins.

    The score is a lexicographic tuple:

    1. **Source priority** — ``foreign_key`` > ``test`` > ``custom`` (an
       enforced constraint outweighs any unverified mapping).
    2. **Known columns** — 0/1/2 sides with real column names.
    3. **Label** — a ``relationship_label`` is present.
    4. **Explicit cardinality** — ``type`` is not the ``n1`` fallback.

    Args:
        ref: Relationship to score.

    Returns:
        Comparable score tuple; the greater tuple has the stronger evidence.

    """
    source_weight = len(SOURCE_PRIORITY) - SOURCE_PRIORITY.get(ref.source, len(SOURCE_PRIORITY))
    known_columns = int(_columns_known(list(ref.column_map[0]))) + int(_columns_known(list(ref.column_map[1])))
    has_label = int(bool(ref.relationship_label))
    explicit_type = int(ref.type != "n1")
    return (source_weight, known_columns, has_label, explicit_type)


def _pair_key(ref: Ref) -> frozenset[str]:
    """Unordered table-pair key; self-references collapse to a single name."""
    return frozenset(ref.table_map)


def merge_duplicate_refs(refs: Optional[list[Ref]] = None) -> list[Ref]:
    """Merge relationships between the same pair of tables into one edge.

    Both exact duplicates and bidirectional duplicates (``A->B`` and ``B->A``)
    are merged: the key is the *unordered* table pair. The surviving ref is
    the one with the highest :func:`evidence_score`; ties keep the first-seen
    ref, preserving its direction and position.

    Args:
        refs: Parsed relationships. Defaults to [].

    Returns:
        One ref per unordered table pair, in first-seen order.

    """
    if not refs:
        return []

    winners: dict[frozenset[str], Ref] = {}
    scores: dict[frozenset[str], tuple[int, int, int, int]] = {}
    for ref in refs:
        key = _pair_key(ref)
        score = evidence_score(ref)
        if key not in winners or score > scores[key]:
            winners[key] = ref
            scores[key] = score

    return list(winners.values())


def split_self_references(refs: Optional[list[Ref]] = None) -> tuple[list[Ref], list[Ref]]:
    """Split relationships into regular ones and self-references.

    Args:
        refs: Parsed relationships. Defaults to [].

    Returns:
        Tuple of (regular relationships, self-referencing relationships).

    """
    if not refs:
        return [], []
    regular = [ref for ref in refs if not ref.is_self_reference]
    self_refs = [ref for ref in refs if ref.is_self_reference]
    return regular, self_refs


def find_cycle(refs: Optional[list[Ref]] = None) -> list[str]:
    """Find one directed cycle among non-self relationships, if any.

    Self-references are ignored: a table referencing itself is not a cycle
    for layout purposes.

    Args:
        refs: Relationships (edge direction is FK side -> referenced side).

    Returns:
        Table names forming a cycle (last repeats the first), or [] if acyclic.

    """
    if not refs:
        return []

    regular, _ = split_self_references(refs)
    adjacency: dict[str, list[str]] = {}
    for ref in regular:
        _, from_table = ref.table_map
        to_table = ref.table_map[0]
        adjacency.setdefault(from_table, []).append(to_table)
        adjacency.setdefault(to_table, [])

    unvisited, in_stack, done = 0, 1, 2
    state = dict.fromkeys(adjacency, unvisited)

    for root, root_children in adjacency.items():
        if state[root] != unvisited:
            continue
        # Iterative DFS; each stack item is (node, iterator over its children).
        stack: list[tuple[str, object]] = [(root, iter(root_children))]
        state[root] = in_stack
        path = [root]
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if state.get(child, done) == in_stack:
                    return [*path[path.index(child) :], child]
                if state.get(child, done) == unvisited:
                    state[child] = in_stack
                    stack.append((child, iter(adjacency.get(child, []))))
                    path.append(child)
                    advanced = True
                    break
            if not advanced:
                state[node] = done
                stack.pop()
                path.pop()

    return []


def has_cycle(refs: Optional[list[Ref]] = None) -> bool:
    """Whether the non-self relationships contain a directed cycle."""
    return bool(find_cycle(refs))


@dataclass
class Subgraph:
    """A connected component of the relationship graph.

    Attributes:
        name: Stable group identifier (``subgraph_1``, ``subgraph_2``, ...).
        tables: Sorted table names in the component.
        relationships: Non-self relationships inside the component.
        self_references: Self-referencing relationships, kept separate so they
            never participate in cycle detection or layout edges.
        is_singleton: True when the component is a single table with no
            relationships to other tables.
        has_cycle: True when the component's relationships contain a cycle.

    """

    name: str
    tables: list[str] = field(default_factory=list)
    relationships: list[Ref] = field(default_factory=list)
    self_references: list[Ref] = field(default_factory=list)
    is_singleton: bool = False
    has_cycle: bool = False


def group_subgraphs(
    tables: Optional[list[Union[Table, str]]] = None,
    refs: Optional[list[Ref]] = None,
) -> list[Subgraph]:
    """Group tables and relationships into connected-component subgraphs.

    Every table lands in exactly one subgraph — isolated tables become
    singleton groups. Self-references are attached to their table's subgraph
    but kept in ``self_references``, excluded from ``has_cycle``.

    Args:
        tables: Tables (or plain table names) to group. Defaults to [].
        refs: Relationships connecting the tables. Defaults to [].

    Returns:
        Subgraphs ordered by their smallest table name, so output is stable.

    """
    if tables is None:
        tables = []
    if refs is None:
        refs = []

    table_names = [table.name if isinstance(table, Table) else str(table) for table in tables]
    regular_refs, self_refs = split_self_references(refs)

    # Union-find over non-self relationships.
    parent = {name: name for name in table_names}

    def find(name: str) -> str:
        root = name
        while parent[root] != root:
            root = parent[root]
        while parent[name] != root:
            parent[name], name = root, parent[name]
        return root

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    for ref in regular_refs:
        to_table, from_table = ref.table_map
        if to_table in parent and from_table in parent:
            union(to_table, from_table)

    components: dict[str, list[str]] = {}
    for name in table_names:
        components.setdefault(find(name), []).append(name)

    refs_by_root: dict[str, list[Ref]] = {root: [] for root in components}
    for ref in regular_refs:
        to_table = ref.table_map[0]
        if to_table in parent:
            refs_by_root.setdefault(find(to_table), []).append(ref)

    self_refs_by_root: dict[str, list[Ref]] = {root: [] for root in components}
    for ref in self_refs:
        table = ref.table_map[0]
        if table in parent:
            self_refs_by_root.setdefault(find(table), []).append(ref)

    subgraphs = []
    ordered_components = sorted(components.items(), key=lambda item: min(item[1]))
    for index, (root, names) in enumerate(ordered_components, start=1):
        relationships = refs_by_root.get(root, [])
        subgraphs.append(
            Subgraph(
                name=f"subgraph_{index}",
                tables=sorted(names),
                relationships=relationships,
                self_references=self_refs_by_root.get(root, []),
                is_singleton=len(names) == 1 and not relationships,
                has_cycle=has_cycle(relationships),
            )
        )

    return subgraphs
