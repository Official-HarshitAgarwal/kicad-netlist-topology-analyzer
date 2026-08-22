"""
KiCad adapter: converts a live ``pcbnew`` board into neutral :mod:`.model` data.

**This is the only module in the project that imports ``pcbnew``.** Everything
else operates on the plain-Python structures defined in :mod:`.model`. That
boundary is what lets the whole analysis engine be unit-tested without KiCad,
and what will make migrating to KiCad's new IPC API a change confined to one
file.

Defensive accessor strategy
---------------------------
The SWIG ``pcbnew`` bindings are stable in broad shape but individual accessor
names have shifted across KiCad 6 -> 7 -> 8 -> 9 (for example a pad's number has
been reachable as ``GetNumber``, ``GetName`` and ``GetPadName`` at different
points, and positions changed from ``wxPoint`` to ``VECTOR2I``). Rather than
binding hard to one spelling, every risky access goes through
:func:`_try_call`, which walks a list of candidate method names and falls back
to a default.

The cost is a little indirection; the benefit is that the plugin degrades
gracefully instead of raising ``AttributeError`` and dying on a version we did
not anticipate. Anything that could not be read is recorded in
``BoardData.meta['extraction_warnings']`` so problems stay visible rather than
silently producing an empty analysis.
"""

from __future__ import annotations

import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    BoardData,
    Component,
    Net,
    Pad,
    TrackSegment,
    build_nets_from_components,
)

# ``pcbnew`` only exists inside KiCad's Python environment. Importing lazily
# keeps this module importable (and testable) anywhere.
try:  # pragma: no cover - depends on runtime environment
    import pcbnew  # type: ignore
except ImportError:  # pragma: no cover
    pcbnew = None  # type: ignore


class ExtractionError(RuntimeError):
    """Raised when no board could be obtained from KiCad."""


# ---------------------------------------------------------------------------
# Low-level defensive helpers
# ---------------------------------------------------------------------------


def _try_call(obj, names, default=None):
    """Call the first existing zero-argument method from ``names``.

    Returns ``default`` if none of the names exist or every call raises.
    """
    for name in names:
        method = getattr(obj, name, None)
        if method is None:
            continue
        try:
            return method()
        except Exception:
            continue
    return default


def _to_str(value, default=""):
    # type: (object, str) -> str
    """Coerce a possibly-``wxString`` value to a plain ``str``."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    return text


def _to_mm(value):
    # type: (object) -> float
    """Convert KiCad internal units (nanometres) to millimetres.

    Prefers KiCad's own converter so we inherit any future unit change; falls
    back to the documented 1 IU = 1 nm relationship used by all of KiCad 6-9.
    """
    if value is None:
        return 0.0
    if pcbnew is not None:
        for name in ("ToMM", "Iu2Millimeter"):
            converter = getattr(pcbnew, name, None)
            if converter is not None:
                try:
                    return float(converter(value))
                except Exception:
                    pass
    try:
        return float(value) / 1e6
    except Exception:
        return 0.0


def _position_mm(item):
    # type: (object) -> Tuple[float, float]
    """Read an item's position as millimetres.

    Handles both the modern ``VECTOR2I`` (``.x``/``.y``) and the legacy
    ``wxPoint`` returned by older bindings.
    """
    pos = _try_call(item, ("GetPosition", "GetCenter"))
    if pos is None:
        return (0.0, 0.0)
    x = getattr(pos, "x", None)
    y = getattr(pos, "y", None)
    if x is None or y is None:
        try:  # some bindings expose a 2-sequence
            x, y = pos[0], pos[1]
        except Exception:
            return (0.0, 0.0)
    return (_to_mm(x), _to_mm(y))


def _pad_number(pad):
    # type: (object) -> str
    """Pad number/name, across all known accessor spellings.

    KiCad treats pad numbers as strings because BGA pads use names like ``A12``.
    """
    value = _try_call(pad, ("GetNumber", "GetPadName", "GetName"), default="")
    return _to_str(value)


def _footprint_name(footprint):
    # type: (object) -> str
    """Library identifier of a footprint, e.g. ``"Package_SO:SOIC-8"``."""
    value = _try_call(footprint, ("GetFPIDAsString",))
    if value:
        return _to_str(value)
    fpid = _try_call(footprint, ("GetFPID",))
    if fpid is not None:
        lib = _to_str(_try_call(fpid, ("GetLibNickname",)), "")
        item = _to_str(_try_call(fpid, ("GetLibItemName", "GetUniStringLibItemName")), "")
        if lib and item:
            return "{0}:{1}".format(lib, item)
        if item:
            return item
    return ""


def _is_dnp(footprint):
    # type: (object) -> bool
    """Whether the part is marked Do-Not-Populate.

    ``IsDNP()`` exists from KiCad 7 onward; older bindings only expose the
    attribute bitmask.
    """
    value = _try_call(footprint, ("IsDNP",))
    if isinstance(value, bool):
        return value
    attributes = _try_call(footprint, ("GetAttributes",))
    flag = getattr(pcbnew, "FP_DNP", None) if pcbnew is not None else None
    if isinstance(attributes, int) and isinstance(flag, int):
        return bool(attributes & flag)
    return False


def _is_excluded_from_bom(footprint):
    # type: (object) -> bool
    value = _try_call(footprint, ("IsExcludedFromBOM",))
    if isinstance(value, bool):
        return value
    attributes = _try_call(footprint, ("GetAttributes",))
    flag = getattr(pcbnew, "FP_EXCLUDE_FROM_BOM", None) if pcbnew is not None else None
    if isinstance(attributes, int) and isinstance(flag, int):
        return bool(attributes & flag)
    return False


def _is_via(track):
    # type: (object) -> bool
    """Whether a board connectivity item is a via rather than a track segment."""
    if pcbnew is not None:
        via_type = getattr(pcbnew, "PCB_VIA", None)
        if via_type is not None:
            try:
                if isinstance(track, via_type):
                    return True
            except Exception:
                pass
    return _to_str(_try_call(track, ("GetClass",)), "").upper().endswith("VIA")


def _layer_name(board, item):
    # type: (object, object) -> str
    name = _try_call(item, ("GetLayerName",))
    if name:
        return _to_str(name)
    layer = _try_call(item, ("GetLayer",))
    if layer is not None and board is not None:
        try:
            return _to_str(board.GetLayerName(layer))
        except Exception:
            pass
    return ""


# ---------------------------------------------------------------------------
# Board acquisition
# ---------------------------------------------------------------------------


def get_current_board():
    """Return the board currently open in the PCB editor.

    Raises :class:`ExtractionError` when running outside KiCad or with no board
    loaded, so callers can show a clear message instead of crashing.
    """
    if pcbnew is None:
        raise ExtractionError(
            "The 'pcbnew' module is unavailable. This plugin must run inside "
            "KiCad's PCB editor (Tools > External Plugins)."
        )
    board = pcbnew.GetBoard()
    if board is None:
        raise ExtractionError("No board is currently open in the PCB editor.")
    return board


def board_display_name(board):
    # type: (object) -> str
    path = _to_str(_try_call(board, ("GetFileName",)), "")
    if not path:
        return "board"
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".kicad_pcb"):
        name = name[: -len(".kicad_pcb")]
    return name or "board"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _extract_components(board, warnings):
    # type: (object, List[str]) -> List[Component]
    components = []
    footprints = _try_call(board, ("GetFootprints", "GetModules"), default=None)
    if footprints is None:
        warnings.append("Could not enumerate footprints on this board.")
        return components

    for footprint in footprints:
        try:
            reference = _to_str(_try_call(footprint, ("GetReference",)), "")
            if not reference:
                continue
            fx, fy = _position_mm(footprint)
            component = Component(
                reference=reference,
                value=_to_str(_try_call(footprint, ("GetValue",)), ""),
                footprint=_footprint_name(footprint),
                x=fx,
                y=fy,
                layer=_layer_name(board, footprint),
                dnp=_is_dnp(footprint),
                excluded_from_bom=_is_excluded_from_bom(footprint),
            )

            pads = _try_call(footprint, ("Pads",), default=None)
            if pads is None:
                warnings.append("Could not read pads of {0}.".format(reference))
            else:
                for pad in pads:
                    px, py = _position_mm(pad)
                    net_code = _try_call(pad, ("GetNetCode",), default=0) or 0
                    component.pads.append(
                        Pad(
                            reference=reference,
                            number=_pad_number(pad),
                            net_name=_to_str(_try_call(pad, ("GetNetname",)), ""),
                            net_code=int(net_code),
                            x=px,
                            y=py,
                        )
                    )
            components.append(component)
        except Exception as exc:
            warnings.append("Skipped a footprint: {0}: {1}".format(type(exc).__name__, exc))
    return components


def _extract_declared_nets(board, warnings):
    # type: (object, List[str]) -> Dict[str, int]
    """Map net name -> net code as declared by the board.

    Several binding spellings are attempted because this is one of the least
    consistent corners of the API. Failure is non-fatal: nets are then derived
    from pad assignments instead, which always works but omits empty nets.
    """
    declared = {}  # type: Dict[str, int]

    # Strategy 1: the name-keyed map.
    container = _try_call(board, ("GetNetsByName",))
    if container is not None:
        try:
            items = container.items()
        except Exception:
            items = None
        if items is not None:
            try:
                for name, net_info in items:
                    net_name = _to_str(
                        _try_call(net_info, ("GetNetname",), default=name), ""
                    )
                    code = _try_call(net_info, ("GetNetCode",), default=0) or 0
                    if net_name:
                        declared[net_name] = int(code)
                if declared:
                    return declared
            except Exception as exc:
                warnings.append("GetNetsByName() iteration failed: {0}".format(exc))

    # Strategy 2: the netcode-keyed map on NETINFO_LIST.
    net_info_list = _try_call(board, ("GetNetInfo",))
    if net_info_list is not None:
        by_code = _try_call(net_info_list, ("NetsByNetcode",))
        if by_code is not None:
            try:
                for code, net_info in by_code.items():
                    net_name = _to_str(_try_call(net_info, ("GetNetname",)), "")
                    if net_name:
                        declared[net_name] = int(code)
                if declared:
                    return declared
            except Exception as exc:
                warnings.append("NetsByNetcode() iteration failed: {0}".format(exc))

    if not declared:
        warnings.append(
            "Could not read the board's net table; nets were derived from pad "
            "assignments instead (nets with no pads will be missing)."
        )
    return declared


def _extract_tracks(board, warnings):
    # type: (object, List[str]) -> List[TrackSegment]
    segments = []
    tracks = _try_call(board, ("GetTracks",), default=None)
    if tracks is None:
        warnings.append("Could not enumerate tracks; routing checks will be skipped.")
        return segments

    for track in tracks:
        try:
            via = _is_via(track)
            segments.append(
                TrackSegment(
                    net_code=int(_try_call(track, ("GetNetCode",), default=0) or 0),
                    net_name=_to_str(_try_call(track, ("GetNetname",)), ""),
                    width_mm=_to_mm(_try_call(track, ("GetWidth",), default=0)),
                    length_mm=(
                        0.0 if via else _to_mm(_try_call(track, ("GetLength",), default=0))
                    ),
                    layer=_layer_name(board, track),
                    is_via=via,
                )
            )
        except Exception as exc:
            warnings.append("Skipped a track: {0}: {1}".format(type(exc).__name__, exc))
    return segments


def extract_board(board=None):
    # type: (Optional[object]) -> BoardData
    """Build a :class:`~.model.BoardData` snapshot from a KiCad board.

    Parameters
    ----------
    board:
        A ``pcbnew.BOARD``. When omitted, the board currently open in the PCB
        editor is used.
    """
    if board is None:
        board = get_current_board()

    warnings = []  # type: List[str]

    components = _extract_components(board, warnings)
    tracks = _extract_tracks(board, warnings)
    declared = _extract_declared_nets(board, warnings)

    # Nets derived from pads are the source of truth for membership; declared
    # nets add any that exist with no pads attached.
    nets = build_nets_from_components(components)
    by_name = dict((n.name, n) for n in nets)
    for name, code in declared.items():
        if not name:
            continue
        if name in by_name:
            by_name[name].code = code
        else:
            nets.append(Net(name=name, code=code))
    nets.sort(key=lambda n: n.name)

    # Fold routing information onto the nets.
    for segment in tracks:
        net = by_name.get(segment.net_name)
        if net is None:
            continue
        if segment.is_via:
            net.via_count += 1
        else:
            net.track_count += 1
            net.track_length_mm += segment.length_mm

    data = BoardData(
        name=board_display_name(board),
        components=components,
        nets=nets,
        tracks=tracks,
    )
    data.meta["extracted_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    data.meta["kicad_version"] = _kicad_version()
    data.meta["board_file"] = _to_str(_try_call(board, ("GetFileName",)), "")
    if warnings:
        data.meta["extraction_warnings"] = " | ".join(warnings[:20])
    return data


def _kicad_version():
    # type: () -> str
    if pcbnew is None:
        return "unavailable"
    for name in ("GetBuildVersion", "FullVersion", "Version"):
        getter = getattr(pcbnew, name, None)
        if getter is None:
            continue
        try:
            return _to_str(getter())
        except Exception:
            continue
    return "unknown"
