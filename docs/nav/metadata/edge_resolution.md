# Edge resolution (deduplication, self references, cycles)

As a dbt project grows, the raw relationships discovered from tests,
constraints and semantic entities produce graph input that is awkward to lay
out:

- the same pair of models is related more than once (duplicate edges);
- a model relates to itself through a self foreign key (self loops);
- two models are related in both directions (two crossing edges);
- many small disconnected components, or accidental cycles, stretch the
  layout.

`dbterd` normalizes every relationship through one deterministic pipeline
before it reaches any target, implemented in
[`dbterd/core/graph.py`](https://github.com/datnguye/dbterd/blob/main/dbterd/core/graph.py).

## Merge rules

Relationships are grouped by the **unordered pair of endpoint models**. Every
group collapses to a single edge:

1. **Repeated same-direction relationships** merge into one edge.
2. **Opposite-direction relationships** merge into one edge as well — never
   two crossing edges. The merged edge keeps the FK-side direction
   (`child -> parent`) and is marked `bidirectional: true` on the JSON target.
3. **Column evidence accumulates**: columns from every losing edge are appended
   to the winner (oriented to the winner's direction when the losing edge
   pointed the other way), so composite/multi-column links stay complete.
4. **Provenance accumulates**: every contributing test/constraint/entity name
   is kept on the edge's `evidence` list.

### Which evidence wins the edge structure

The winning edge provides the structural fields — direction, cardinality
(`type`) and label — chosen by:

1. **Evidence completeness first.** The edge with the most distinct evidence
   points wins; the number of pinned columns breaks a near-tie.
2. **Source priority then.** When evidence is equally complete, the discovery
   source with the higher priority wins (see below).
3. **First-seen order last.** Guarantees a stable result when everything ties.

## Source priority

When the same relationship is discoverable through multiple channels, the
priority order (highest first) is:

| Priority | Source (`edge.source`) | Meaning |
| --- | --- | --- |
| 1 | `custom` | Explicitly authored custom mappings / overrides — human intent wins. |
| 2 | `foreign_key` | Enforced model-contract `foreign_key` constraints (dbt >= 1.9) — schema-level truth. |
| 3 | `semantic` | Semantic Layer primary/foreign entity pairs — a declared join path. |
| 4 | `test` | `relationships` data tests — inferred, sometimes present only for data-quality checks. |

Source priority only breaks ties between equally complete candidates; a
richer-evidence edge always wins regardless of source. The priorities live on
`RefSource.priority` in
[`dbterd/core/models.py`](https://github.com/datnguye/dbterd/blob/main/dbterd/core/models.py).

## Self references

A relationship whose two endpoints are the same model (self FK, e.g.
`employees.manager_id -> employees.id`) is:

- merged on its own and returned/flagged separately with
  `is_self_reference: true`;
- still rendered as an edge (so the column link is not lost);
- **never** counted as a cycle, and **never** merged with another component
  during subgraph analysis.

## Subgraphs (single tables and cycles)

The non-self-reference graph is split into connected components. Each
component is emitted on the JSON target as an entry under `subgraphs`:

- `is_single_node: true` — an isolated table with no relationships;
- `has_cycle: true` — a directed cycle exists between non-self edges in the
  component;
- `node_ids` lists members and `edge_ids` lists the merged evidence ids;
- self-reference edges attached to members appear under
  `self_reference_edge_ids`.

Downstream renderers can use these flags to lay out isolated groups
independently and to highlight (or break) cycles instead of letting them
stretch the whole diagram.
