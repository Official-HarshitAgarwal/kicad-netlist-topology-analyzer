"""
Core analysis package - KiCad-independent except for :mod:`.board_extractor`.

Layering (a module may only import from layers above it):

1. :mod:`.model`      - data structures, no logic
2. :mod:`.config`     - tunable thresholds and naming patterns
3. :mod:`.algorithms` - generic graph theory, no domain concepts
4. :mod:`.classifier` - assigns electrical roles to nets
5. :mod:`.graph_model`- builds graph views from board data
6. :mod:`.analyzers`  - domain rules producing findings
7. :mod:`.engine`     - orchestration
8. :mod:`.report`     - text / HTML / JSON rendering

:mod:`.board_extractor` sits outside this stack as the KiCad adapter.
"""

from __future__ import annotations

from .config import AnalysisConfig
from .engine import analyze_board, analyzer_names, available_analyzers
from .graph_model import NetlistGraph
from .model import (
    AnalysisResult,
    BoardData,
    Component,
    Finding,
    Net,
    NetRole,
    Pad,
    Severity,
    TrackSegment,
)
from .report import render_html, render_json, render_svg, render_text

__all__ = [
    "AnalysisConfig",
    "AnalysisResult",
    "BoardData",
    "Component",
    "Finding",
    "Net",
    "NetRole",
    "NetlistGraph",
    "Pad",
    "Severity",
    "TrackSegment",
    "analyze_board",
    "analyzer_names",
    "available_analyzers",
    "render_html",
    "render_json",
    "render_svg",
    "render_text",
]
