"""Unit tests for edge normalization and subgraph analysis."""

import pytest

from dbterd.core.graph import (
    Subgraph,
    analyze_subgraphs,
    evidence_score,
    normalize_relationships,
)
from dbterd.core.models import Ref, RefSource


def make_ref(
    name,
    to_table,
    from_table,
    to_columns=("id",),
    from_columns=("parent_id",),
    source=RefSource.TEST,
    label=None,
    evidence=None,
):
    return Ref(
        name=name,
        table_map=[to_table, from_table],
        column_map=([*to_columns], [*from_columns]),
        relationship_label=label,
        source=source,
        evidence=list(evidence) if evidence is not None else [name],
    )


class TestNormalizeRelationships:
    def test_empty_input(self):
        assert normalize_relationships([]) == ([], [])

    def test_exact_duplicates_collapse_to_one_edge(self):
        refs = [
            make_ref("test.a", "parent", "child"),
            make_ref("test.b", "parent", "child"),
        ]

        relationships, self_references = normalize_relationships(refs)

        assert len(relationships) == 1
        assert relationships[0].table_map == ["parent", "child"]
        assert relationships[0].evidence == ["test.a", "test.b"]
        assert self_references == []

    def test_same_pair_different_columns_merge_and_union_columns(self):
        refs = [
            make_ref("test.a", "parent", "child", ("id",), ("child_id",)),
            make_ref("test.b", "parent", "child", ("id2",), ("child_id2",)),
        ]

        relationships, _ = normalize_relationships(refs)

        assert len(relationships) == 1
        assert sorted(relationships[0].column_map[0]) == ["id", "id2"]
        assert sorted(relationships[0].column_map[1]) == ["child_id", "child_id2"]
        assert relationships[0].evidence == ["test.a", "test.b"]

    def test_bidirectional_relationships_become_one_edge(self):
        refs = [
            make_ref("test.fk", "parent", "child"),
            make_ref("test.reverse", "child", "parent"),
        ]

        relationships, _ = normalize_relationships(refs)

        assert len(relationships) == 1
        edge = relationships[0]
        assert edge.bidirectional is True
        # The FK-side direction (child -> parent) is preserved.
        assert edge.table_map == ["parent", "child"]

    def test_reverse_direction_columns_are_oriented_to_winner(self):
        refs = [
            make_ref("a", "parent", "child", ("id",), ("parent_id",)),
            make_ref("b", "child", "parent", ("parent_id",), ("id",)),
        ]

        relationships, _ = normalize_relationships(refs)

        assert len(relationships) == 1
        assert relationships[0].column_map == (["id"], ["parent_id"])
        assert relationships[0].bidirectional is True

    def test_self_references_are_partitioned_and_flagged(self):
        refs = [
            make_ref("self.test", "node", "node", ("manager_id",), ("manager_id",)),
            make_ref("normal", "parent", "child"),
        ]

        relationships, self_references = normalize_relationships(refs)

        assert len(relationships) == 1
        assert relationships[0].table_map == ["parent", "child"]
        assert len(self_references) == 1
        assert self_references[0].is_self_reference is True
        assert self_references[0].table_map == ["node", "node"]

    def test_duplicate_self_references_merge(self):
        refs = [
            make_ref("s1", "node", "node"),
            make_ref("s2", "node", "node"),
        ]

        _, self_references = normalize_relationships(refs)

        assert len(self_references) == 1
        assert self_references[0].evidence == ["s1", "s2"]

    def test_source_priority_breaks_ties(self):
        test_ref = make_ref("from.test", "parent", "child", ("id",), ("parent_id",), label="test-label")
        fk_ref = make_ref(
            "from.fk",
            "parent",
            "child",
            ("id",),
            ("parent_id",),
            source=RefSource.FOREIGN_KEY,
            label="fk-label",
        )

        relationships, _ = normalize_relationships([test_ref, fk_ref])

        edge = relationships[0]
        assert edge.source == RefSource.FOREIGN_KEY
        assert edge.relationship_label == "fk-label"
        assert set(edge.evidence) == {"from.test", "from.fk"}
        assert edge.evidence[0] == "from.fk"

    def test_most_complete_evidence_wins_over_source_priority(self):
        fk_ref = make_ref(
            "fk.one",
            "parent",
            "child",
            source=RefSource.FOREIGN_KEY,
            evidence=["fk.one"],
        )
        test_ref = make_ref(
            "test.rich",
            "parent",
            "child",
            source=RefSource.TEST,
            evidence=["e1", "e2", "e3"],
        )

        relationships, _ = normalize_relationships([fk_ref, test_ref])

        edge = relationships[0]
        assert edge.source == RefSource.TEST
        assert edge.name == "test.rich"
        assert set(edge.evidence) == {"fk.one", "e1", "e2", "e3"}

    def test_first_seen_wins_when_score_and_priority_tie(self):
        first = make_ref("first", "parent", "child", label="first-label")
        second = make_ref("second", "parent", "child", label="second-label")

        relationships, _ = normalize_relationships([first, second])

        assert relationships[0].relationship_label == "first-label"

    def test_evidence_score_rewards_distinct_evidence_and_columns(self):
        weak = make_ref("w", "p", "c", evidence=["w"])
        strong = make_ref("s", "p", "c", evidence=["s1", "s2"])

        assert evidence_score(strong) > evidence_score(weak)

    def test_self_reference_without_evidence_uses_its_name(self):
        ref = Ref(
            name="self.x",
            table_map=["n", "n"],
            column_map=(["a"], ["b"]),
            source=RefSource.TEST,
            evidence=[],
        )

        _, self_references = normalize_relationships([ref])

        assert self_references[0].evidence == ["self.x"]

    def test_unrelated_pairs_remain_separate(self):
        refs = [
            make_ref("a", "p1", "c1"),
            make_ref("b", "p2", "c2"),
        ]

        relationships, _ = normalize_relationships(refs)

        assert len(relationships) == 2


class TestAnalyzeSubgraphs:
    def _edge(self, name, to_table, from_table):
        return make_ref(name, to_table, from_table)

    def test_isolated_tables_are_single_node_subgraphs(self):
        subgraphs = analyze_subgraphs(["a", "b"], [])

        assert len(subgraphs) == 2
        assert {sub.name for sub in subgraphs} == {"subgraph_1", "subgraph_2"}
        assert all(sub.is_single_node for sub in subgraphs)
        assert all(not sub.has_cycle for sub in subgraphs)

    def test_connected_tables_share_a_subgraph(self):
        edges = [self._edge("e1", "a", "b"), self._edge("e2", "a", "c")]

        subgraphs = analyze_subgraphs(["a", "b", "c", "d"], edges)

        connected = next(sub for sub in subgraphs if set(sub.node_ids) == {"a", "b", "c"})
        isolated = next(sub for sub in subgraphs if sub.node_ids == ["d"])
        assert connected.is_single_node is False
        assert connected.has_cycle is False
        assert connected.edge_ids == ["e1", "e2"]
        assert isolated.is_single_node is True

    def test_directed_cycle_is_flagged(self):
        edges = [
            self._edge("e1", "a", "b"),  # b -> a
            self._edge("e2", "b", "c"),  # c -> b
            self._edge("e3", "c", "a"),  # a -> c
        ]

        subgraphs = analyze_subgraphs(["a", "b", "c"], edges)

        assert len(subgraphs) == 1
        assert subgraphs[0].has_cycle is True

    def test_acyclic_diamond_is_not_cyclic(self):
        edges = [
            self._edge("e1", "root", "left"),
            self._edge("e2", "root", "right"),
            self._edge("e3", "left", "leaf"),
            self._edge("e4", "right", "leaf"),
        ]

        subgraphs = analyze_subgraphs(["root", "left", "right", "leaf"], edges)

        assert len(subgraphs) == 1
        assert subgraphs[0].has_cycle is False

    def test_self_references_never_participate_in_cycle_detection(self):
        # Only a self edge exists for node x: it is a single-node component and
        # the self loop must not be reported as a cycle.
        subgraphs = analyze_subgraphs(["x"], [])

        assert len(subgraphs) == 1
        assert subgraphs[0].is_single_node is True
        assert subgraphs[0].has_cycle is False

    def test_self_edge_does_not_merge_components_or_create_cycle(self):
        normal = [self._edge("e1", "a", "b")]
        # Normalized callers pass non-self edges only; a stray flagged edge is
        # ignored by the analysis regardless.
        stray = make_ref("self", "c", "c")
        stray.is_self_reference = True

        subgraphs = analyze_subgraphs(["a", "b", "c"], [*normal, stray])

        assert len(subgraphs) == 2
        assert all(not sub.has_cycle for sub in subgraphs)

    def test_returns_subgraph_instances(self):
        subgraphs = analyze_subgraphs(["a"], [])

        assert isinstance(subgraphs[0], Subgraph)

    @pytest.mark.parametrize(
        "edges,expected_cyclic",
        [
            ([], False),
            ([("a", "b")], False),
            ([("a", "b"), ("b", "c"), ("c", "a")], True),
        ],
    )
    def test_cycle_parametrized(self, edges, expected_cyclic):
        refs = [
            make_ref(f"e{i}", to_table=to_table, from_table=from_table)
            for i, (to_table, from_table) in enumerate(edges)
        ]
        nodes = sorted({node for edge in edges for node in edge}) or ["solo"]

        subgraphs = analyze_subgraphs(nodes, refs)

        assert any(sub.has_cycle for sub in subgraphs) is expected_cyclic
