"""
Shared helpers for building small synthetic boards in tests.

Constructing :class:`~netlist_topology_analyzer.core.model.BoardData` by hand is
what makes the engine testable without KiCad. These helpers keep the test bodies
focused on the behaviour under test rather than on object construction.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netlist_topology_analyzer.core.model import (  # noqa: E402
    BoardData,
    Component,
    Pad,
    TrackSegment,
    build_nets_from_components,
)


def mk_component(reference, pads, value="", x=0.0, y=0.0, footprint="", **kwargs):
    """Build a component. ``pads`` is a sequence of ``(number, net_name)``."""
    component = Component(
        reference=reference,
        value=value,
        footprint=footprint,
        x=float(x),
        y=float(y),
        **kwargs
    )
    for number, net in pads:
        component.pads.append(
            Pad(
                reference=reference,
                number=str(number),
                net_name=net,
                net_code=0 if not net else 1,
                x=float(x),
                y=float(y),
            )
        )
    return component


def mk_board(components, name="test_board", route_all=False, unrouted=()):
    """Assemble a board, assigning net codes the way KiCad does.

    ``route_all`` synthesises one track per multi-pad net so that routing checks
    see a routed board; nets named in ``unrouted`` are left without copper.
    """
    nets = build_nets_from_components(components)
    for index, net in enumerate(nets, start=1):
        net.code = index
        for pad in net.pads:
            pad.net_code = index

    board = BoardData(name=name, components=components, nets=nets)

    if route_all:
        for net in nets:
            if net.pad_count < 2 or net.name in unrouted:
                continue
            board.tracks.append(
                TrackSegment(
                    net_code=net.code,
                    net_name=net.name,
                    width_mm=0.25,
                    length_mm=5.0,
                    layer="F.Cu",
                )
            )
            net.track_count = 1
            net.track_length_mm = 5.0
    return board


def simple_powered_board():
    """A minimal but electrically complete board used by several tests.

    ``U1`` is an 8-pad IC on ``+3V3``/``GND`` with a decoupling capacitor ``C1``
    placed 2 mm away, an I2C pair to sensor ``U2``, and a connector ``J1``.
    """
    components = [
        mk_component(
            "U1",
            [(1, "+3V3"), (2, "GND"), (3, "SDA"), (4, "SCL"),
             (5, "TX"), (6, "GND"), (7, "+3V3"), (8, "")],
            value="MCU",
            x=10.0,
            y=10.0,
        ),
        mk_component("C1", [(1, "+3V3"), (2, "GND")], value="100nF", x=10.0, y=12.0),
        mk_component(
            "U2",
            [(1, "+3V3"), (2, "GND"), (3, "SDA"), (4, "SCL"),
             (5, "GND"), (6, "+3V3"), (7, "GND"), (8, "")],
            value="SENSOR",
            x=20.0,
            y=10.0,
        ),
        mk_component("C2", [(1, "+3V3"), (2, "GND")], value="100nF", x=20.0, y=12.0),
        mk_component("R1", [(1, "SDA"), (2, "+3V3")], value="4k7", x=15.0, y=6.0),
        mk_component("R2", [(1, "SCL"), (2, "+3V3")], value="4k7", x=17.0, y=6.0),
        mk_component("J1", [(1, "TX"), (2, "GND")], value="HEADER", x=30.0, y=10.0),
    ]
    return mk_board(components, name="simple_powered_board", route_all=True)


def find(findings, code):
    """All findings with a given code."""
    return [f for f in findings if f.code == code]


def codes(findings):
    """Set of finding codes present."""
    return set(f.code for f in findings)
