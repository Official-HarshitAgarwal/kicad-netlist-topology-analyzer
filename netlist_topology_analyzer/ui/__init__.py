"""wxPython user-interface layer.

Isolated from the analysis code so the engine can run head-less. Everything here
imports ``wx`` defensively, because this package must stay importable outside
KiCad for testing.
"""

from __future__ import annotations
