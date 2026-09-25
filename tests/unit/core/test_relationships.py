from dbterd.core.models import Ref, Table
from dbterd.core.relationships import (
    RefSource,
    evidence_score,
    find_cycle,
    group_subgraphs,
    has_cycle,
    merge_duplicate_refs,
    split_self_references,
)


def _ref(
    name: str,
    to: str,
    from_: str,
    to_columns: list[str] | None = None,
    from_columns: list[str] | None = None,
    type: str = "n1",
    label: str | None = None,
    source: str = "custom",
) -> Ref:
    return Ref(
        name=name,
        table_map=(to, from_),
        column_map=(
            to_columns if to_columns is not None else ["id"],
            from_columns if from_columns is not None else ["id"],
        ),
        type=type,
        relationship_label=label,
        source=source,
    )


class TestEvidenceScore:
    def test_source_priority_dominates(self):
        fk = _ref("fk", "a", "b", source=RefSource.FOREIGN_KEY.value)
        test = _ref("test", "a", "b", source=RefSource.TEST.value, label="labeled", type="11")
        custom = _ref("custom", "a", "b", source=RefSource.CUSTOM.value, label="labeled", type="11")
        assert evidence_score(fk) > evidence_score(test) > evidence_score(custom)

    def test_unknown_source_sorts_last(self):
        unknown = _ref("x", "a", "b", source="something-else")
        custom = _ref("c", "a", "b", source=RefSource.CUSTOM.value)
        assert evidence_score(custom) > evidence_score(unknown)

    def test_completeness_breaks_ties_within_source(self):
        bare = _ref("bare", "a", "b", to_columns=["unknown"], from_columns=[""])
        full = _ref("full", "a", "b", label="belongs to", type="1n")
        assert evidence_score(full) > evidence_score(bare)


class TestMergeDuplicateRefs:
    def test_empty_and_none(self):
        assert merge_duplicate_refs(refs=None) == []
        assert merge_duplicate_refs(refs=[]) == []

    def test_exact_duplicates_collapse(self):
        refs = [_ref("r1", "a", "b"), _ref("r2", "a", "b")]
        merged = merge_duplicate_refs(refs=refs)
        assert len(merged) == 1
        assert merged[0].name == "r1"  # equal evidence: first seen wins

    def test_bidirectional_pair_collapses(self):
        refs = [_ref("r1", "a", "b"), _ref("r2", "b", "a")]
        merged = merge_duplicate_refs(refs=refs)
        assert len(merged) == 1

    def test_most_complete_evidence_wins(self):
        bare = _ref("bare", "a", "b", to_columns=["unknown"])
        full = _ref("full", "b", "a", label="belongs to", type="1n")
        merged = merge_duplicate_refs(refs=[bare, full])
        assert [ref.name for ref in merged] == ["full"]
        # ...regardless of input order
        merged = merge_duplicate_refs(refs=[full, bare])
        assert [ref.name for ref in merged] == ["full"]

    def test_higher_priority_source_wins(self):
        custom = _ref("custom", "a", "b", label="labeled", type="11", source=RefSource.CUSTOM.value)
        fk = _ref("fk", "b", "a", source=RefSource.FOREIGN_KEY.value)
        merged = merge_duplicate_refs(refs=[custom, fk])
        assert [ref.name for ref in merged] == ["fk"]

    def test_distinct_pairs_are_kept(self):
        refs = [_ref("r1", "a", "b"), _ref("r2", "a", "c"), _ref("r3", "b", "c")]
        assert len(merge_duplicate_refs(refs=refs)) == 3

    def test_self_reference_pairs_merge_separately(self):
        refs = [_ref("self1", "a", "a"), _ref("self2", "a", "a"), _ref("normal", "a", "b")]
        merged = merge_duplicate_refs(refs=refs)
        assert len(merged) == 2
        assert {ref.name for ref in merged} == {"self1", "normal"}


class TestSelfReferences:
    def test_is_self_reference(self):
        assert _ref("s", "a", "a").is_self_reference
        assert not _ref("n", "a", "b").is_self_reference

    def test_split(self):
        refs = [_ref("s", "a", "a"), _ref("n", "a", "b")]
        regular, self_refs = split_self_references(refs=refs)
        assert [ref.name for ref in regular] == ["n"]
        assert [ref.name for ref in self_refs] == ["s"]

    def test_split_empty(self):
        assert split_self_references(refs=None) == ([], [])


class TestCycleDetection:
    def test_no_cycle(self):
        refs = [_ref("r1", "a", "b"), _ref("r2", "b", "c")]
        assert not has_cycle(refs=refs)
        assert find_cycle(refs=refs) == []

    def test_cycle(self):
        refs = [_ref("r1", "a", "b"), _ref("r2", "b", "c"), _ref("r3", "c", "a")]
        assert has_cycle(refs=refs)
        cycle = find_cycle(refs=refs)
        assert cycle[0] == cycle[-1]
        assert set(cycle[:-1]) == {"a", "b", "c"}

    def test_self_reference_is_not_a_cycle(self):
        refs = [_ref("s", "a", "a")]
        assert not has_cycle(refs=refs)
        assert find_cycle(refs=refs) == []

    def test_empty(self):
        assert not has_cycle(refs=None)
        assert find_cycle(refs=None) == []


class TestGroupSubgraphs:
    @staticmethod
    def _table(name: str) -> Table:
        return Table(name=name, node_name=name, database="db", schema="sch")

    def test_empty(self):
        assert group_subgraphs(tables=None, refs=None) == []

    def test_singletons_and_connected_components(self):
        tables = [self._table(name) for name in ["a", "b", "c", "lonely"]]
        refs = [_ref("r1", "a", "b"), _ref("r2", "b", "c")]
        subgraphs = group_subgraphs(tables=tables, refs=refs)
        assert len(subgraphs) == 2

        connected = next(sg for sg in subgraphs if not sg.is_singleton)
        assert connected.tables == ["a", "b", "c"]
        assert [ref.name for ref in connected.relationships] == ["r1", "r2"]
        assert not connected.has_cycle

        lonely = next(sg for sg in subgraphs if sg.is_singleton)
        assert lonely.tables == ["lonely"]
        assert lonely.relationships == []

    def test_cyclic_component_is_flagged(self):
        tables = [self._table(name) for name in ["a", "b", "c"]]
        refs = [_ref("r1", "a", "b"), _ref("r2", "b", "c"), _ref("r3", "c", "a")]
        (subgraph,) = group_subgraphs(tables=tables, refs=refs)
        assert subgraph.has_cycle
        assert not subgraph.is_singleton

    def test_self_reference_grouped_but_not_cyclic(self):
        tables = [self._table("a"), self._table("b")]
        refs = [_ref("self", "a", "a"), _ref("r1", "a", "b")]
        (subgraph,) = group_subgraphs(tables=tables, refs=refs)
        assert [ref.name for ref in subgraph.self_references] == ["self"]
        assert [ref.name for ref in subgraph.relationships] == ["r1"]
        assert not subgraph.has_cycle

    def test_standalone_self_reference_is_singleton_without_cycle(self):
        tables = [self._table("a")]
        refs = [_ref("self", "a", "a")]
        (subgraph,) = group_subgraphs(tables=tables, refs=refs)
        assert subgraph.is_singleton
        assert not subgraph.has_cycle
        assert [ref.name for ref in subgraph.self_references] == ["self"]

    def test_accepts_plain_table_names(self):
        subgraphs = group_subgraphs(tables=["a", "b"], refs=[_ref("r1", "a", "b")])
        assert len(subgraphs) == 1
        assert subgraphs[0].tables == ["a", "b"]

    def test_stable_names_and_ordering(self):
        tables = [self._table(name) for name in ["z", "m", "a"]]
        subgraphs = group_subgraphs(tables=tables, refs=[])
        assert [sg.name for sg in subgraphs] == ["subgraph_1", "subgraph_2", "subgraph_3"]
        assert [sg.tables for sg in subgraphs] == [["a"], ["m"], ["z"]]
