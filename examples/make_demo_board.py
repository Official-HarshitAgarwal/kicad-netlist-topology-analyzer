"""
Build the demo board snapshot shipped in ``examples/demo_board.json``.

The demo models a plausible ESP32-class sensor board: a USB input, an LDO, an
MCU, an I2C sensor bus, an SPI EEPROM on a filtered rail, a crystal, an
opto-isolated output, connectors, and passives.

It is deliberately seeded with defects so that every analyzer has something to
report. Keeping the generator in the repository (rather than only the JSON)
documents *why* each finding appears, which makes the example usable as a
regression baseline:

===========================  ==================================================
Seeded defect                Expected finding
===========================  ==================================================
``J3``/``R6`` isolated loop  NTA-100 board splits into 2 electrical islands
``D2`` with no pad nets      NTA-101 component has no connected pads
``TP_SPARE`` one pad only    NTA-110 net reaches only one pad
``SPI_MISO``/``UART_RX``     NTA-120 net is unrouted
``SDA``/``SCL`` 9+ pads      NTA-130 high-fanout signal net
``U4`` on ``+3V3_MEM``       NTA-144 no decoupling capacitor
``U8`` far from ``C7``       NTA-145 nearest decoupling capacitor too far
``U1`` hub                   NTA-150 single point of failure, NTA-160 centrality
``H1``/``H2`` mounting holes filtered out entirely (not reported as orphans)
===========================  ==================================================

Run ``python examples/make_demo_board.py`` to regenerate the JSON.
"""

from __future__ import annotations

import io
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

#: Nets left deliberately without copper, to exercise the routing check.
UNROUTED_NETS = ("SPI_MISO", "UART_RX")

#: Rails get a wider track, as they would on a real board.
RAIL_NETS = ("+3V3", "+5V", "GND", "+3V3_MEM", "GND_ISO")


def part(reference, value, footprint, x, y, pads, **kwargs):
    """Create a component. ``pads`` maps pad number -> net name ('' = no net)."""
    component = Component(
        reference=reference,
        value=value,
        footprint=footprint,
        x=float(x),
        y=float(y),
        layer="F.Cu",
        **kwargs
    )
    for number, net in pads:
        component.pads.append(
            Pad(
                reference=reference,
                number=str(number),
                net_name=net,
                # Net codes are assigned later; 0 means "no net" in KiCad.
                net_code=0 if not net else 1,
                x=float(x),
                y=float(y),
            )
        )
    return component


def build_components():
    components = []

    # ---- power input and regulation ---------------------------------------
    components.append(
        part(
            "J1", "USB_C", "Connector:USB_C_Receptacle", 10, 40,
            [(1, "+5V"), (2, "USB_DM"), (3, "USB_DP"), (4, "SHIELD"), (5, "GND")],
        )
    )
    components.append(
        part(
            "U2", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223", 25, 40,
            [(1, "GND"), (2, "+3V3"), (3, "+5V"), (4, "+3V3")],
        )
    )
    components.append(part("C1", "10uF", "Capacitor_SMD:C_0805", 25, 48,
                          [(1, "+5V"), (2, "GND")]))
    components.append(part("C5", "100nF", "Capacitor_SMD:C_0402", 52, 44,
                          [(1, "+3V3"), (2, "GND")]))
    components.append(part("C6", "100nF", "Capacitor_SMD:C_0402", 48, 44,
                          [(1, "+3V3"), (2, "GND")]))

    # ---- MCU ---------------------------------------------------------------
    mcu_pads = [
        (1, "+3V3"), (2, "+3V3"), (3, "GND"), (4, "GND"),
        (5, "SDA"), (6, "SCL"),
        (7, "SPI_SCK"), (8, "SPI_MOSI"), (9, "SPI_MISO"), (10, "SPI_CS"),
        (11, "UART_TX"), (12, "UART_RX"),
        (13, "XTAL1"), (14, "XTAL2"),
        (15, "nRESET"), (16, "LED_A"),
        (17, "USB_DM"), (18, "USB_DP"),
        (19, "OPTO_DRV"),
        (20, "GPIO0"), (21, "GPIO1"), (22, "GPIO2"), (23, "GPIO3"),
        (24, "GPIO4"), (25, "GPIO5"),
        (26, "GND"), (27, "GND"), (28, "+3V3"),
        (29, ""), (30, ""),  # genuinely unconnected pins
        (31, "GND"), (32, "GND"),
    ]
    components.append(
        part("U1", "ESP32-WROOM", "RF_Module:ESP32-WROOM-32", 50, 40, mcu_pads)
    )

    # ---- crystal -----------------------------------------------------------
    components.append(
        part("Y1", "16MHz", "Crystal:Crystal_SMD_3225-4Pin", 40, 30,
             [(1, "XTAL1"), (2, "GND"), (3, "XTAL2"), (4, "GND")])
    )
    components.append(part("C2", "22pF", "Capacitor_SMD:C_0402", 38, 26,
                          [(1, "XTAL1"), (2, "GND")]))
    components.append(part("C3", "22pF", "Capacitor_SMD:C_0402", 42, 26,
                          [(1, "XTAL2"), (2, "GND")]))

    # ---- I2C bus: four sensors plus pull-ups -------------------------------
    # SDA/SCL end up with 9+ pads each, which is what trips the fanout check.
    components.append(part("R1", "4k7", "Resistor_SMD:R_0402", 60, 20,
                          [(1, "SDA"), (2, "+3V3")]))
    components.append(part("R2", "4k7", "Resistor_SMD:R_0402", 64, 20,
                          [(1, "SCL"), (2, "+3V3")]))

    sensor_layout = (
        ("U3", "BME280", 70, 30, "C4", 70, 34),
        ("U6", "MPU6050", 70, 68, "C8", 70, 72),
        ("U7", "TMP117", 95, 40, "C9", 95, 44),
        ("U9", "DS3231", 95, 25, "C10", 95, 29),
        # U8 sits far from every capacitor -> triggers the distance advisory.
        ("U8", "SHT31", 70, 95, "C7", 85, 95),
    )
    for ref, value, x, y, cap_ref, cap_x, cap_y in sensor_layout:
        components.append(
            part(ref, value, "Package_LGA:LGA-8", x, y,
                 [(1, "+3V3"), (2, "GND"), (3, "SDA"), (4, "SCL"),
                  (5, "GND"), (6, "+3V3"), (7, "GND"), (8, "")])
        )
        components.append(
            part(cap_ref, "100nF", "Capacitor_SMD:C_0402", cap_x, cap_y,
                 [(1, "+3V3"), (2, "GND")])
        )

    components.append(part("TP2", "TestPoint", "TestPoint:TestPoint_Pad_D1.0mm",
                          62, 16, [(1, "SDA")]))

    # ---- SPI EEPROM on a separately filtered rail --------------------------
    # +3V3_MEM is fed through FB1 and has no capacitor of its own, so U4 has a
    # recognised power rail but no decoupling: finding NTA-144.
    components.append(part("FB1", "600R", "Inductor_SMD:L_0603", 62, 55,
                          [(1, "+3V3"), (2, "+3V3_MEM")]))
    components.append(
        part("U4", "AT25SF081", "Package_SO:SOIC-8", 70, 55,
             [(1, "SPI_CS"), (2, "SPI_MISO"), (3, ""), (4, "GND"),
              (5, "SPI_MOSI"), (6, "SPI_SCK"), (7, "+3V3_MEM"), (8, "+3V3_MEM")])
    )

    # ---- reset, status LED -------------------------------------------------
    components.append(part("R3", "10k", "Resistor_SMD:R_0402", 44, 50,
                          [(1, "nRESET"), (2, "+3V3")]))
    components.append(part("SW1", "SW_Push", "Button_Switch_SMD:SW_SPST_SKQG",
                          40, 54, [(1, "nRESET"), (2, "GND")]))
    components.append(part("R4", "1k", "Resistor_SMD:R_0402", 56, 52,
                          [(1, "LED_A"), (2, "LED_K")]))
    components.append(part("D1", "LED_GREEN", "LED_SMD:LED_0603", 60, 52,
                          [(1, "LED_K"), (2, "GND")]))

    # Seeded defect: a status LED whose pads were never assigned to nets.
    components.append(part("D2", "LED_RED", "LED_SMD:LED_0603", 64, 52,
                          [(1, ""), (2, "")]))

    # ---- opto-isolated output ---------------------------------------------
    components.append(part("R5", "330R", "Resistor_SMD:R_0402", 88, 70,
                          [(1, "OPTO_DRV"), (2, "OPTO_A")]))
    components.append(
        part("U5", "PC817", "Package_DIP:DIP-4_W7.62mm", 95, 70,
             [(1, "OPTO_A"), (2, "GND"), (3, "OUT_ISO"), (4, "GND_ISO")])
    )
    components.append(
        part("J2", "Conn_02x03", "Connector_PinHeader:PinHeader_2x03", 105, 45,
             [(1, "+3V3"), (2, "GND"), (3, "SDA"), (4, "SCL"),
              (5, "UART_TX"), (6, "OUT_ISO")])
    )

    # GPIO breakout header: keeps the spare MCU pins on real two-pad nets, as a
    # practical design would, instead of leaving six dangling labels.
    components.append(
        part("J4", "Conn_01x08", "Connector_PinHeader:PinHeader_1x08", 30, 20,
             [(1, "GPIO0"), (2, "GPIO1"), (3, "GPIO2"), (4, "GPIO3"),
              (5, "GPIO4"), (6, "GPIO5"), (7, "UART_RX"), (8, "GND")])
    )

    # Seeded defect: a spare test point on a net with no other member.
    components.append(part("TP1", "TestPoint", "TestPoint:TestPoint_Pad_D1.0mm",
                           30, 60, [(1, "TP_SPARE")]))

    # ---- seeded defect: a genuinely isolated sense loop --------------------
    # J3 and R6 touch nothing else on the board, so the netlist splits into a
    # second electrical island.
    components.append(part("J3", "Conn_01x02", "Connector_PinHeader:PinHeader_1x02",
                           15, 95, [(1, "ISO_A"), (2, "ISO_B")]))
    components.append(part("R6", "100R", "Resistor_SMD:R_0402", 22, 95,
                          [(1, "ISO_A"), (2, "ISO_B")]))

    # ---- mechanical: must be filtered out, not flagged as orphans ----------
    components.append(part("H1", "MountingHole", "MountingHole:MountingHole_3.2mm",
                           5, 5, []))
    components.append(part("H2", "MountingHole", "MountingHole:MountingHole_3.2mm",
                           115, 105, []))

    return components


def build_tracks(nets):
    """Synthesise one plausible track per routed multi-pad net."""
    tracks = []
    for net in nets:
        if net.pad_count < 2 or net.name in UNROUTED_NETS:
            continue
        # Rough proxy for routed length: spread of the pads it joins.
        xs = [p.x for p in net.pads]
        ys = [p.y for p in net.pads]
        span = (max(xs) - min(xs)) + (max(ys) - min(ys))
        length = max(2.0, span * 1.2)
        width = 0.50 if net.name in RAIL_NETS else 0.25
        segments = max(1, net.pad_count - 1)
        for _ in range(segments):
            tracks.append(
                TrackSegment(
                    net_code=net.code,
                    net_name=net.name,
                    width_mm=width,
                    length_mm=round(length / segments, 3),
                    layer="F.Cu",
                )
            )
    return tracks


def build_board():
    components = build_components()
    nets = build_nets_from_components(components)

    # Assign stable net codes the way KiCad does (1-based; 0 means no net).
    for index, net in enumerate(nets, start=1):
        net.code = index
        for pad in net.pads:
            pad.net_code = index

    tracks = build_tracks(nets)

    board = BoardData(
        name="demo_sensor_board",
        components=components,
        nets=nets,
        tracks=tracks,
    )
    for segment in tracks:
        net = board.net_by_name(segment.net_name)
        if net is not None:
            net.track_count += 1
            net.track_length_mm = round(net.track_length_mm + segment.length_mm, 3)

    board.meta["source"] = "examples/make_demo_board.py"
    board.meta["note"] = (
        "Synthetic board with intentionally seeded defects; see the module "
        "docstring for the expected findings."
    )
    return board


def main():
    board = build_board()
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_board.json")
    with io.open(target, "w", encoding="utf-8") as handle:
        handle.write(board.to_json())
    sys.stdout.write(
        "wrote {0}\n  {1} components, {2} nets, {3} track segments\n".format(
            target, len(board.components), len(board.nets), len(board.tracks)
        )
    )


if __name__ == "__main__":
    main()
