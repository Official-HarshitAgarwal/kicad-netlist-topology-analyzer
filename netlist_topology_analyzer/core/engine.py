"""
The analysis engine: orchestrates one complete analysis run.

Pipeline
--------
::

    BoardData
       |
       v
    [1] classify_board      assign POWER / GROUND / SIGNAL / UNCONNECTED roles
       |
       v
    [2] NetlistGraph        build bipartite incidence + rail-free projection
       |
       v
    [3] analyzers           each rule contributes Findings, metrics, sections
       |
       v
    AnalysisResult          consumed by the GUI and the report writers

The ordering is a genuine dependency chain, not an arbitrary sequence:
classification must precede graph construction because the projection needs to
know which nets are rails, and the analyzers need both.

Robustness
----------
Analyzers run inside individual ``try``/``except`` blocks. A single rule hitting
an unexpected board construct records an error string in the result and the run
continues, so the user still gets every other finding. Silently swallowing the
failure would be worse - it is surfaced in the report's errors list.
"""

from __future__ import annotations

import time
import traceback
from typing import List, Optional

from .analyzers import REGISTRY, AnalysisContext, Analyzer
from .classifier import classify_board
from .config import AnalysisConfig
from .graph_model import NetlistGraph
from .model import AnalysisResult, BoardData


def available_analyzers():
    # type: () -> List[type]
    """Analyzer classes in execution order."""
    return list(REGISTRY)


def analyzer_names():
    # type: () -> List[str]
    return [cls.name for cls in REGISTRY]


def select_analyzers(config):
    # type: (AnalysisConfig) -> List[type]
    """Resolve the configured analyzer subset, preserving registry order.

    An empty ``enabled_analyzers`` means "run everything", which keeps the
    common case configuration-free.
    """
    if not config.enabled_analyzers:
        return list(REGISTRY)
    wanted = set(config.enabled_analyzers)
    return [cls for cls in REGISTRY if cls.name in wanted]


def analyze_board(board, config=None):
    # type: (BoardData, Optional[AnalysisConfig]) -> AnalysisResult
    """Run the full pipeline over ``board`` and return the result.

    This function is the single public entry point of the engine. The GUI, the
    command-line runner, and the tests all call exactly this, which guarantees
    they can never diverge in behaviour.
    """
    cfg = config or AnalysisConfig()
    result = AnalysisResult(board_name=board.name)
    started = time.time()

    # [1] Classification -------------------------------------------------
    try:
        histogram = classify_board(board, cfg)
        result.metrics["net_role_histogram"] = histogram
    except Exception as exc:  # pragma: no cover - defensive
        result.errors.append("Net classification failed: {0}".format(exc))

    # [2] Graph construction ---------------------------------------------
    graph = NetlistGraph(board, cfg)

    # [3] Analyzers ------------------------------------------------------
    ctx = AnalysisContext(board=board, graph=graph, config=cfg, result=result)
    for analyzer_cls in select_analyzers(cfg):
        analyzer = analyzer_cls()  # type: Analyzer
        try:
            result.extend(list(analyzer.analyze(ctx)) or [])
        except Exception as exc:
            result.errors.append(
                "Analyzer '{0}' failed: {1}: {2}".format(
                    analyzer_cls.name, type(exc).__name__, exc
                )
            )
            result.errors.append(traceback.format_exc(limit=3))

    result.metrics["analysis_time_s"] = round(time.time() - started, 3)
    result.metrics["analyzers_run"] = len(select_analyzers(cfg))
    summary = result.count_by_severity()
    result.metrics["errors_found"] = summary.get("ERROR", 0)
    result.metrics["warnings_found"] = summary.get("WARNING", 0)
    return result
