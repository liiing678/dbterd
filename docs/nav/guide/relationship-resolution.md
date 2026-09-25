# Relationship Resolution

dbterd can extract relationships from several dbt features at once, and real projects
routinely report the *same* pair of models more than once — a relationship test plus a
model contract, a test written in each direction, or simply duplicate tests. Left
unhandled, those show up as parallel edges, self-loops, and doubled arrows that wreck the
diagram layout. Before any target renders the result, dbterd normalizes the relationship
list with the rules on this page.

## Duplicate merging

Relationships are keyed by the **unordered table pair**: `A → B` and `B → A` are the same
pair, so a bidirectional relationship collapses into a single edge instead of two. Exact
duplicates (same pair, same columns) collapse the same way.

When several relationships compete for one pair, the survivor is the one with the
**most complete evidence**, scored lexicographically:

1. **Source priority** (see below) — an enforced constraint outweighs a declared mapping.
2. **Known columns** — relationships naming real columns beat `unknown` placeholders.
3. **Label** — a `relationship_label` is a plus.
4. **Explicit cardinality** — any configured `relationship_type` beats the `n1` fallback.

Ties keep the first-seen relationship, preserving its direction. The merge runs for every
algorithm (`test_relationship`, `model_contract`, `semantic`) and every target.

## Source priority

Relationships carry a `source` recording where they came from. When sources disagree
about a table pair, the more authoritative source wins:

| Priority | Source        | Produced by       | Why it ranks there                                        |
| -------- | ------------- | ----------------- | --------------------------------------------------------- |
| 1 (max)  | `foreign_key` | `model_contract`  | Warehouse-enforced (or validated) constraint              |
| 2        | `test`        | `test_relationship` | Verified by `dbt test` — but only when the suite runs   |
| 3 (min)  | `custom`      | `semantic`, other | Declared mapping (semantic entities, metadata); unverified |

## Self-references

A recursive relationship (a model referencing itself, e.g. `employee.manager_id →
employee.id`) is marked with `is_self_reference: true` on the JSON edge and is **excluded
from cycle detection** — a self-loop is not a layout-breaking cycle. Self-referencing
edges still render as normal edges in the diagram targets.

## Subgraph grouping

The `json` target emits a `subgraphs` array: the connected components of the
relationship graph. Every table lands in exactly one subgraph:

- **Isolated tables** form singleton subgraphs (`is_singleton: true`), so single-table
  islands can be laid out (or hidden) as a group.
- **Cyclic components** are flagged with `has_cycle: true`, computed over directed edges
  with self-references ignored — layout engines can special-case those groups instead of
  choking on them.
- Self-referencing edges are listed separately per subgraph
  (`self_reference_edge_ids`) and never count toward `has_cycle`.

Subgraphs are named `subgraph_1`, `subgraph_2`, ... ordered by their smallest table
name, so the output is stable across runs.
