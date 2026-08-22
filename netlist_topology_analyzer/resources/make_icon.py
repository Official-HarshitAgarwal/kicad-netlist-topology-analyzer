"""
Generate the plugin's toolbar icon.

KiCad expects a small PNG (24x24 is the toolbar convention) at the absolute path
given by ``ActionPlugin.icon_file_name``. The icon is generated rather than
hand-drawn so it is reproducible and reviewable as code.

The motif is the plugin's subject matter: a central hub node joined to
satellites, with one satellite highlighted to suggest a flagged part.

Run ``python netlist_topology_analyzer/resources/make_icon.py`` to regenerate.
Pillow is needed only for this script, never at plugin runtime.
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw

#: Supersampling factor - draw large, downsample for cheap anti-aliasing.
SCALE = 8
SIZE = 24
CANVAS = SIZE * SCALE

EDGE_COLOUR = (128, 138, 152, 255)
NODE_COLOUR = (59, 110, 165, 255)
HUB_COLOUR = (26, 68, 116, 255)
FLAG_COLOUR = (193, 68, 14, 255)


def build():
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    centre = CANVAS / 2.0
    radius = CANVAS * 0.33
    satellites = []
    for index in range(5):
        angle = 2.0 * math.pi * index / 5.0 - math.pi / 2.0
        satellites.append(
            (centre + radius * math.cos(angle), centre + radius * math.sin(angle))
        )

    # Edges: hub to every satellite, plus one ring edge to imply a cycle.
    width = max(1, int(1.4 * SCALE))
    for x, y in satellites:
        draw.line([(centre, centre), (x, y)], fill=EDGE_COLOUR, width=width)
    draw.line([satellites[1], satellites[2]], fill=EDGE_COLOUR, width=width)

    def disc(x, y, r, colour):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=colour)

    node_r = CANVAS * 0.085
    for index, (x, y) in enumerate(satellites):
        disc(x, y, node_r, FLAG_COLOUR if index == 3 else NODE_COLOUR)
    disc(centre, centre, CANVAS * 0.135, HUB_COLOUR)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
    build().save(target, "PNG", optimize=True)
    print("wrote {0}".format(target))


if __name__ == "__main__":
    main()
