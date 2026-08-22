"""
Tunable configuration for the analysis engine.

Every heuristic threshold and every pattern used to recognise power/ground nets
lives here rather than being hard-coded inside an analyzer. That keeps the
electronics domain knowledge in one auditable place, and it means a user can
adapt the plugin to a house style (e.g. rails named ``P3V3`` instead of ``+3V3``)
without touching algorithm code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Net-name patterns
# ---------------------------------------------------------------------------
#
# Regexes are matched case-insensitively against the *last* path element of a
# hierarchical net name, because KiCad prefixes nets inside a hierarchical sheet
# with the sheet path, e.g. ``/power_supply/+3V3``.
#
# Ground is tested before power: names such as ``VSS`` and ``GNDA`` would
# otherwise be caught by the broad ``V...`` power patterns.

DEFAULT_GROUND_PATTERNS = (
    r"^GND",             # GND, GNDA, GND_1, GNDPWR
    r"^A?GND",           # AGND
    r"^[DP]GND",         # DGND, PGND
    r"^VSS",             # VSS, VSSA
    r"^0V$",
    r"^EARTH$",
    r"^CHASSIS",
    r"^VEE$",            # negative supply, treated as a rail
    r"GROUND",
)

DEFAULT_POWER_PATTERNS = (
    r"^VCC",             # VCC, VCCIO
    r"^VDD",             # VDD, VDDA
    r"^VBUS$",
    r"^VBAT",
    r"^VIN$",
    r"^VOUT$",
    r"^VREF",
    r"^VVDD",
    r"^AVDD",
    r"^DVDD",
    r"^\+?\d+V\d*$",     # 5V, +5V, 3V3, +3V3, 12V
    r"^\+?\d+V\d*_",     # +3V3_MCU
    r"^-\d+V\d*$",       # -5V, -12V
    r"^V\d+V\d+$",       # V3V3
    r"^P\d+V\d+$",       # P3V3 (house style)
    r"^PWR",
    r"^POWER$",
    r"^RAIL",
)

# ---------------------------------------------------------------------------
# Reference-designator classes (IEEE 315 / IEC 81346 prefixes)
# ---------------------------------------------------------------------------

#: Prefixes for parts that behave as decoupling/bypass capacitors.
DEFAULT_CAPACITOR_PREFIXES = ("C", "CAP")

#: Prefixes for active parts that normally require decoupling.
DEFAULT_IC_PREFIXES = ("U", "IC", "Q", "AR", "RN")

#: Prefixes for connectors / test points, which legitimately have low fanout
#: and are frequent (and benign) articulation points.
DEFAULT_CONNECTOR_PREFIXES = ("J", "P", "CN", "CON", "TP", "X", "SW", "K")

#: Prefixes for mechanical-only parts that carry no electrical meaning.
DEFAULT_MECHANICAL_PREFIXES = ("H", "MK", "MH", "FID", "LOGO", "G")


@dataclass
class AnalysisConfig:
    """Thresholds and switches controlling the analysis run."""

    # -- net classification ------------------------------------------------

    ground_patterns: Tuple[str, ...] = DEFAULT_GROUND_PATTERNS
    power_patterns: Tuple[str, ...] = DEFAULT_POWER_PATTERNS

    #: A net touching at least this fraction of all components is treated as a
    #: rail even if its name does not match a pattern. Catches house-style names
    #: like ``MAIN_SUPPLY``. Set to a value > 1.0 to disable.
    rail_fanout_ratio: float = 0.60

    #: The fanout heuristic above is only trusted on boards with at least this
    #: many components, since on a 3-part board *every* net looks like a rail.
    rail_fanout_min_components: int = 8

    # -- fanout analysis ---------------------------------------------------

    #: Signal nets with more than this many pads are flagged as high-fanout
    #: (stub/reflection and loading risk on unterminated nets).
    high_fanout_threshold: int = 8

    #: Above this, the finding is escalated from WARNING to ERROR severity.
    critical_fanout_threshold: int = 16

    # -- decoupling analysis ----------------------------------------------

    #: Only ICs with at least this many pads are checked for decoupling, to
    #: avoid flagging every 3-pin regulator or transistor.
    decoupling_min_ic_pads: int = 6

    #: Maximum distance (mm) from an IC at which a capacitor still counts as
    #: "local" decoupling. ~10 mm is a common practical guideline; the physical
    #: rationale is keeping the power-loop inductance small.
    decoupling_max_distance_mm: float = 10.0

    #: When True, a capacitor anywhere on the same power/ground net pair counts,
    #: and distance is reported as advisory information only.
    decoupling_ignore_distance: bool = False

    # -- topology analysis -------------------------------------------------

    #: Exclude power/ground nets when projecting the signal-topology graph.
    #: Rails connect nearly every part, so leaving them in makes the projection
    #: almost complete and destroys all structural information.
    exclude_rails_from_projection: bool = True

    #: Nets with more pads than this are skipped when projecting the component
    #: graph. A net with N pads contributes O(N^2) projected edges, so a large
    #: bus would dominate both the runtime and the resulting structure without
    #: adding real insight.
    projection_max_net_fanout: int = 32

    #: Betweenness centrality is O(V*E); skip it above this component count to
    #: keep the GUI responsive.
    centrality_max_components: int = 400

    #: How many top-ranked entries each ranked section reports.
    top_n: int = 10

    #: Upper bound on individual findings a single rule may emit. Beyond this,
    #: the rule emits the first N and one summary finding covering the rest.
    #: A report that buries the reader in near-identical warnings gets skimmed
    #: and ignored, which defeats the point of running the checks.
    max_findings_per_rule: int = 12

    # -- routing analysis --------------------------------------------------

    #: Report nets that have >= 2 pads but no copper at all.
    check_unrouted: bool = True

    # -- part classification ----------------------------------------------

    capacitor_prefixes: Tuple[str, ...] = DEFAULT_CAPACITOR_PREFIXES
    ic_prefixes: Tuple[str, ...] = DEFAULT_IC_PREFIXES
    connector_prefixes: Tuple[str, ...] = DEFAULT_CONNECTOR_PREFIXES
    mechanical_prefixes: Tuple[str, ...] = DEFAULT_MECHANICAL_PREFIXES

    #: Skip DNP / BOM-excluded parts during analysis.
    ignore_dnp: bool = True

    # -- which analyzers to run -------------------------------------------
    #
    # Empty means "all registered analyzers". Names refer to
    # ``Analyzer.name`` values in :mod:`analyzers`.
    enabled_analyzers: List[str] = field(default_factory=list)

    # -- compiled-pattern cache -------------------------------------------

    def __post_init__(self):
        self._ground_re = [re.compile(p, re.IGNORECASE) for p in self.ground_patterns]
        self._power_re = [re.compile(p, re.IGNORECASE) for p in self.power_patterns]

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def leaf_net_name(net_name):
        # type: (str) -> str
        """Strip a hierarchical sheet path from a net name.

        ``"/power/+3V3"`` -> ``"+3V3"``. KiCad also emits auto-generated names
        such as ``"Net-(U1-Pad3)"``, which are left untouched.
        """
        if not net_name:
            return ""
        name = net_name.strip()
        if "/" in name and not name.startswith("Net-("):
            name = name.rsplit("/", 1)[-1]
        return name

    def matches_ground(self, net_name):
        # type: (str) -> bool
        leaf = self.leaf_net_name(net_name)
        return any(rx.search(leaf) for rx in self._ground_re)

    def matches_power(self, net_name):
        # type: (str) -> bool
        leaf = self.leaf_net_name(net_name)
        return any(rx.search(leaf) for rx in self._power_re)

    def _has_prefix(self, reference_prefix, prefixes):
        # type: (str, Tuple[str, ...]) -> bool
        # Exact match on the whole prefix, not a startswith test: the prefix
        # extractor already returns "CN" for "CN1", so a startswith test would
        # wrongly make every "CN..." part a capacitor via the "C" entry.
        return reference_prefix in set(p.upper() for p in prefixes)

    def is_capacitor(self, prefix):
        # type: (str) -> bool
        return self._has_prefix(prefix, self.capacitor_prefixes)

    def is_ic(self, prefix):
        # type: (str) -> bool
        return self._has_prefix(prefix, self.ic_prefixes)

    def is_connector(self, prefix):
        # type: (str) -> bool
        return self._has_prefix(prefix, self.connector_prefixes)

    def is_mechanical(self, prefix):
        # type: (str) -> bool
        return self._has_prefix(prefix, self.mechanical_prefixes)
