from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class Column:
    """Parsed Column object."""

    name: str = "unknown"
    data_type: str = "unknown"
    description: str = ""
    is_primary_key: bool = False


@dataclass
class Table:
    """Parsed Table object."""

    name: str
    database: str
    schema: str
    columns: Optional[list[Column]] = None
    raw_sql: Optional[str] = None
    resource_type: str = "model"
    exposures: list[str] = field(default_factory=list)
    node_name: Optional[str] = None
    description: str = ""
    label: Optional[str] = None
    raw_depends_on: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


class RefSource(Enum):
    """Where a relationship was discovered, with its merge priority.

    When the same pair of models is related through more than one source,
    the source with the higher priority wins the structural fields of the
    single merged edge (direction, cardinality, label). Every contributing
    source remains attached to the edge via ``Ref.evidence``, so no signal is
    lost. Priority order, highest first:

    1. ``custom`` — explicitly authored mappings (`meta`/override configs);
       these express human intent and therefore override inferred edges.
    2. ``foreign_key`` — enforced model-contract `foreign_key` constraints;
       schema-enforced truth from dbt >= 1.9.
    3. ``semantic`` — Semantic Layer primary/foreign entity pairs; a declared
       join path, but one tier below the physical constraint.
    4. ``test`` — `relationships` data tests; inferred, and often added for
       data-quality checks rather than to document the logical model.
    """

    CUSTOM = "custom"
    FOREIGN_KEY = "foreign_key"
    SEMANTIC = "semantic"
    TEST = "test"
    UNKNOWN = "unknown"

    @property
    def priority(self) -> int:
        """Merge priority; higher wins. See the class docstring for rationale."""
        return {
            RefSource.CUSTOM: 40,
            RefSource.FOREIGN_KEY: 30,
            RefSource.SEMANTIC: 20,
            RefSource.TEST: 10,
            RefSource.UNKNOWN: 0,
        }[self]


@dataclass
class Ref:
    """Parsed Relationship object.

    ``table_map`` / ``column_map`` are ordered ``(to, from)``: index 0 is the
    referenced (parent) side, index 1 is the FK (child) side.

    Attributes:
        source: Discovery channel of the edge; drives merge priority.
        evidence: Provenance identifiers (test/constraint/entity names) of
            every raw relationship that collapsed into this edge.
        is_self_reference: Both endpoints are the same model (self FK). Kept
            in its own bucket by edge normalization and excluded from cycle
            detection and subgraph grouping.
        bidirectional: The raw evidence contained edges in both directions;
            the single merged edge points FK-side (``from``) -> parent (``to``).
    """

    name: str
    table_map: tuple[str, str]
    column_map: tuple[list[str], list[str]]
    type: str = "n1"
    relationship_label: Optional[str] = None
    source: RefSource = RefSource.TEST
    evidence: list[str] = field(default_factory=list)
    is_self_reference: bool = False
    bidirectional: bool = False


@dataclass
class SemanticEntity:
    """Parsed Semantic Model's Entity object."""

    semantic_model: str
    model: str
    entity_name: str
    entity_type: str
    column_name: str
    relationship_type: str


class SelectionType(Enum):
    START_WITH_NAME = ""
    EXACT_NAME = "exact"
    SCHEMA = "schema"
    WILDCARD = "wildcard"
    EXPOSURE = "exposure"
