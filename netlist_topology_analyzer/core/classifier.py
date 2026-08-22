"""
Net classification: assigning an electrical role to every net.

This is the first stage of the pipeline and the one that injects most of the
electronics domain knowledge. Everything downstream depends on knowing which
nets are power rails, because rails behave completely differently from signals:
they are high-fanout by design, they legitimately connect nearly every part, and
they must be excluded from signal-topology projections.

Two independent strategies are combined:

1. **Name-based matching** (primary). Rail naming is strongly conventional -
   ``GND``, ``VCC``, ``VDD``, ``+3V3``, ``VBUS``, ``VSS`` - so a curated pattern
   set in :mod:`.config` recognises the overwhelming majority of real designs.
   Ground is tested before power because names like ``VSS`` and ``GNDA`` would
   otherwise be swallowed by the broader ``V...`` power patterns.

2. **Fanout-based inference** (fallback). A net that touches most of the
   components on the board is almost certainly a rail whatever it is called,
   which catches house styles the patterns miss. This heuristic is only trusted
   on boards with enough components to make "most" meaningful - on a four-part
   board every net looks like a rail.

Nets that KiCad reports with no pads, or with KiCad's ``unconnected-`` prefix,
are marked :attr:`~.model.NetRole.UNCONNECTED` and excluded from graph building.
"""

from __future__ import annotations

from typing import Dict, Optional

from .config import AnalysisConfig
from .model import BoardData, Net, NetRole

#: KiCad names nets it created for unconnected pads with this prefix.
KICAD_UNCONNECTED_PREFIXES = ("unconnected-", "Net-(unconnected")


def classify_net(net, config, component_count=0):
    # type: (Net, AnalysisConfig, int) -> str
    """Return the :class:`~.model.NetRole` for a single net."""
    name = (net.name or "").strip()

    if not name or net.pad_count == 0:
        return NetRole.UNCONNECTED

    leaf = config.leaf_net_name(name)
    lowered = leaf.lower()
    if any(lowered.startswith(p.lower()) for p in KICAD_UNCONNECTED_PREFIXES):
        return NetRole.UNCONNECTED

    # A net reaching only one pad cannot carry current anywhere. It is still a
    # "signal" by role; the floating-net analyzer reports it separately, so we
    # deliberately do not hide it as UNCONNECTED here.

    # Ground before power - ordering matters (see module docstring).
    if config.matches_ground(name):
        return NetRole.GROUND
    if config.matches_power(name):
        return NetRole.POWER

    # Fallback: extreme fanout implies a rail.
    if (
        component_count >= config.rail_fanout_min_components
        and config.rail_fanout_ratio <= 1.0
    ):
        reach = len(net.references())
        if reach >= max(2, int(round(config.rail_fanout_ratio * component_count))):
            return NetRole.POWER

    return NetRole.SIGNAL


def classify_board(board, config=None):
    # type: (BoardData, Optional[AnalysisConfig]) -> Dict[str, int]
    """Assign a role to every net on ``board``, in place.

    Returns a histogram of role -> count, which the report uses directly.
    """
    cfg = config or AnalysisConfig()

    # Only electrically meaningful parts count towards the fanout heuristic,
    # so mounting holes and DNP parts cannot skew the ratio.
    relevant = [
        c
        for c in board.components
        if not cfg.is_mechanical(c.prefix)
        and not (cfg.ignore_dnp and (c.dnp or c.excluded_from_bom))
    ]
    component_count = len(relevant)

    histogram = dict((role, 0) for role in NetRole.ALL)
    for net in board.nets:
        net.role = classify_net(net, cfg, component_count)
        histogram[net.role] = histogram.get(net.role, 0) + 1
    return histogram
