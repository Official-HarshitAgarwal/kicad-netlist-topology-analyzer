"""
Neutral, KiCad-independent data model for the Netlist Topology Analyzer.

Design rationale
----------------
Nothing in this module imports ``pcbnew``. The KiCad-specific extraction code
(:mod:`netlist_topology_analyzer.core.board_extractor`) is the *only* place that
touches the KiCad API; it converts a live board into the plain-Python structures
defined here.

This "hexagonal"/ports-and-adapters split buys us three concrete things:

1. **Testability** - the whole analysis engine can be exercised with synthetic
   netlists in a plain CPython interpreter, with no KiCad installed (see
   ``tests/``).
2. **Portability** - when KiCad's new IPC API replaces the SWIG ``pcbnew``
   bindings, only the extractor needs rewriting, not the algorithms.
3. **Reusability** - the same engine can consume a netlist from a file, a
   different EDA tool, or a unit-test fixture.

All geometry is stored in millimetres (floats). The extractor is responsible for
converting KiCad's internal units (nanometres) into millimetres so that the rest
of the codebase never has to think about unit scaling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Net role taxonomy
# ---------------------------------------------------------------------------


class NetRole(object):
    """Electrical role assigned to a net by the classifier.

    Implemented as a class of string constants rather than :class:`enum.Enum`
    so that roles serialise straight to JSON and remain readable in reports.
    """

    POWER = "POWER"
    GROUND = "GROUND"
    SIGNAL = "SIGNAL"
    UNCONNECTED = "UNCONNECTED"

    #: Roles that form the power-distribution network. These are excluded from
    #: signal-topology projections because they are connected to nearly every
    #: component and would otherwise mask the real signal structure.
    RAIL_ROLES = (POWER, GROUND)

    ALL = (POWER, GROUND, SIGNAL, UNCONNECTED)


class Severity(object):
    """Severity levels for findings, ordered from most to least serious."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"

    ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

    @classmethod
    def rank(cls, severity):
        # type: (str) -> int
        """Sort key for a severity string (lower == more severe)."""
        return cls.ORDER.get(severity, 99)


# ---------------------------------------------------------------------------
# Primitive board entities
# ---------------------------------------------------------------------------


@dataclass
class Pad:
    """A single pad (pin) belonging to a component.

    Attributes
    ----------
    reference:
        Reference designator of the owning component, e.g. ``"U1"``.
    number:
        Pad number/name as a string, e.g. ``"1"`` or ``"A12"``. KiCad treats
        pad numbers as strings because BGA pads use alphanumeric names.
    net_name:
        Name of the net attached to this pad. Empty string means "no net".
    net_code:
        KiCad's integer net code. ``0`` is KiCad's "no net" sentinel.
    x, y:
        Pad centre position in millimetres, board coordinates.
    """

    reference: str
    number: str
    net_name: str = ""
    net_code: int = 0
    x: float = 0.0
    y: float = 0.0

    @property
    def uid(self):
        # type: () -> str
        """Stable human-readable pad identifier, e.g. ``"U1.7"``."""
        return "{0}.{1}".format(self.reference, self.number)

    @property
    def is_connected(self):
        # type: () -> bool
        """True when the pad is assigned to a real (non-zero) net."""
        return self.net_code != 0 and bool(self.net_name)


@dataclass
class Component:
    """A footprint / physical part on the board.

    ``pads`` holds every pad of the part, including mechanically-present but
    electrically-unconnected ones, so that pin-count based heuristics (used to
    tell ICs apart from two-terminal passives) stay accurate.
    """

    reference: str
    value: str = ""
    footprint: str = ""
    pads: List[Pad] = field(default_factory=list)
    x: float = 0.0
    y: float = 0.0
    layer: str = ""
    #: True for parts KiCad excludes from the BOM / marked DNP.
    excluded_from_bom: bool = False
    dnp: bool = False

    @property
    def pad_count(self):
        # type: () -> int
        return len(self.pads)

    def net_names(self):
        # type: () -> Set[str]
        """Distinct net names this component touches."""
        return set(p.net_name for p in self.pads if p.is_connected)

    @property
    def prefix(self):
        # type: () -> str
        """Alphabetic prefix of the reference designator (``"U1"`` -> ``"U"``).

        The reference-designator prefix is the standard way (IEEE 315 /
        IEC 81346) of naming a part class, so it is a reliable, footprint-
        independent signal for "is this a capacitor / an IC / a connector".
        """
        letters = []
        for ch in self.reference:
            if ch.isalpha():
                letters.append(ch)
            else:
                break
        return "".join(letters).upper()


@dataclass
class Net:
    """An electrical net: a set of pads that are nominally at the same potential."""

    name: str
    code: int = 0
    pads: List[Pad] = field(default_factory=list)
    role: str = NetRole.SIGNAL
    #: Total routed copper length in millimetres (0.0 when unrouted).
    track_length_mm: float = 0.0
    #: Number of copper track/arc segments assigned to this net.
    track_count: int = 0
    #: Number of vias on this net.
    via_count: int = 0

    @property
    def pad_count(self):
        # type: () -> int
        return len(self.pads)

    @property
    def fanout(self):
        # type: () -> int
        """Number of pads on the net, i.e. its electrical fanout."""
        return len(self.pads)

    def references(self):
        # type: () -> Set[str]
        """Distinct component references attached to this net."""
        return set(p.reference for p in self.pads)

    @property
    def is_rail(self):
        # type: () -> bool
        return self.role in NetRole.RAIL_ROLES

    @property
    def is_routed(self):
        # type: () -> bool
        return self.track_count > 0


@dataclass
class TrackSegment:
    """A copper track segment or via, used for routing-completeness checks."""

    net_code: int
    net_name: str
    width_mm: float = 0.0
    length_mm: float = 0.0
    layer: str = ""
    is_via: bool = False


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------


@dataclass
class BoardData:
    """Complete, tool-neutral snapshot of a board's connectivity.

    This is the single input to the analysis engine. It is intentionally a dumb
    data container: all interpretation happens in the analyzers.
    """

    name: str = "board"
    components: List[Component] = field(default_factory=list)
    nets: List[Net] = field(default_factory=list)
    tracks: List[TrackSegment] = field(default_factory=list)
    #: Free-form provenance info (KiCad version, file path, extraction time).
    meta: Dict[str, str] = field(default_factory=dict)

    # -- lookup helpers ---------------------------------------------------

    def component_by_ref(self, reference):
        # type: (str) -> Optional[Component]
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    def net_by_name(self, name):
        # type: (str) -> Optional[Net]
        for net in self.nets:
            if net.name == name:
                return net
        return None

    def component_map(self):
        # type: () -> Dict[str, Component]
        return dict((c.reference, c) for c in self.components)

    def net_map(self):
        # type: () -> Dict[str, Net]
        return dict((n.name, n) for n in self.nets)

    def nets_with_role(self, *roles):
        # type: (*str) -> List[Net]
        wanted = set(roles)
        return [n for n in self.nets if n.role in wanted]

    def all_pads(self):
        # type: () -> Iterable[Pad]
        for comp in self.components:
            for pad in comp.pads:
                yield pad

    # -- statistics -------------------------------------------------------

    @property
    def pad_count(self):
        # type: () -> int
        return sum(c.pad_count for c in self.components)

    @property
    def connected_pad_count(self):
        # type: () -> int
        return sum(1 for p in self.all_pads() if p.is_connected)

    # -- (de)serialisation ------------------------------------------------
    #
    # JSON round-tripping lets us ship reproducible example boards in the repo
    # and run the engine head-less in CI without KiCad.

    def to_dict(self):
        # type: () -> Dict
        return asdict(self)

    def to_json(self, indent=2):
        # type: (int) -> str
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data):
        # type: (Dict) -> "BoardData"
        """Rebuild a :class:`BoardData` from a plain dict.

        Nets hold *references* to the same :class:`Pad` objects owned by their
        components, matching how :func:`build_nets_from_components` works, so
        that identity comparisons behave consistently either way.
        """
        components = []
        for cdata in data.get("components", []):
            pads = [Pad(**pdata) for pdata in cdata.get("pads", [])]
            kwargs = dict(cdata)
            kwargs["pads"] = pads
            components.append(Component(**kwargs))

        board = cls(
            name=data.get("name", "board"),
            components=components,
            tracks=[TrackSegment(**t) for t in data.get("tracks", [])],
            meta=dict(data.get("meta", {})),
        )

        # Prefer explicit net records (they carry roles and routing stats);
        # fall back to deriving nets purely from pad assignments.
        net_records = data.get("nets")
        if net_records:
            pad_index = {}  # type: Dict[Tuple[str, str], Pad]
            for comp in components:
                for pad in comp.pads:
                    pad_index[(comp.reference, pad.number)] = pad
            nets = []
            for ndata in net_records:
                kwargs = dict(ndata)
                pad_refs = kwargs.pop("pads", [])
                resolved = []
                for pdata in pad_refs:
                    key = (pdata.get("reference", ""), pdata.get("number", ""))
                    resolved.append(pad_index.get(key) or Pad(**pdata))
                kwargs["pads"] = resolved
                nets.append(Net(**kwargs))
            board.nets = nets
        else:
            board.nets = build_nets_from_components(components)
        return board

    @classmethod
    def from_json(cls, text):
        # type: (str) -> "BoardData"
        return cls.from_dict(json.loads(text))


def build_nets_from_components(components):
    # type: (List[Component]) -> List[Net]
    """Derive the net list from pad net assignments.

    Used both by the extractor (as a cross-check) and when loading fixtures
    that only describe components and their pad nets. Nets are returned sorted
    by name so that reports are deterministic.
    """
    by_name = {}  # type: Dict[str, Net]
    for comp in components:
        for pad in comp.pads:
            if not pad.is_connected:
                continue
            net = by_name.get(pad.net_name)
            if net is None:
                net = Net(name=pad.net_name, code=pad.net_code)
                by_name[pad.net_name] = net
            net.pads.append(pad)
    return [by_name[k] for k in sorted(by_name)]


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single diagnostic produced by an analyzer.

    Findings are the common currency between the analysis engine and every
    output surface (GUI list, HTML report, JSON export). Keeping them uniform
    means a new analyzer automatically shows up everywhere without any
    reporting code changing - the Open/Closed principle in practice.
    """

    code: str
    title: str
    severity: str = Severity.INFO
    category: str = "General"
    detail: str = ""
    #: Component references and/or net names this finding points at.
    refs: List[str] = field(default_factory=list)
    #: Optional numeric value that drove the finding (fanout, hop count, ...).
    value: Optional[float] = None

    def to_dict(self):
        # type: () -> Dict
        return asdict(self)


@dataclass
class AnalysisResult:
    """Everything one analysis run produced."""

    board_name: str = "board"
    findings: List[Finding] = field(default_factory=list)
    #: Scalar metrics keyed by name, rendered as the report's summary table.
    metrics: Dict[str, object] = field(default_factory=dict)
    #: Per-analyzer free-form tables/details for the report.
    sections: Dict[str, object] = field(default_factory=dict)
    #: Non-fatal problems hit while running analyzers.
    errors: List[str] = field(default_factory=list)

    def add(self, finding):
        # type: (Finding) -> None
        self.findings.append(finding)

    def extend(self, findings):
        # type: (Iterable[Finding]) -> None
        self.findings.extend(findings)

    def sorted_findings(self):
        # type: () -> List[Finding]
        """Findings ordered by severity, then category, then code."""
        return sorted(
            self.findings,
            key=lambda f: (Severity.rank(f.severity), f.category, f.code),
        )

    def count_by_severity(self):
        # type: () -> Dict[str, int]
        counts = {Severity.ERROR: 0, Severity.WARNING: 0, Severity.INFO: 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def to_dict(self):
        # type: () -> Dict
        return {
            "board_name": self.board_name,
            "metrics": self.metrics,
            "sections": self.sections,
            "errors": self.errors,
            "findings": [f.to_dict() for f in self.sorted_findings()],
            "summary": self.count_by_severity(),
        }

    def to_json(self, indent=2):
        # type: (int) -> str
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, default=str)
