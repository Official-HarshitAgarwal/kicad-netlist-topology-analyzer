# Installation, Execution and Testing

Two ways to run this plugin. **Option A** needs no KiCad at all and is the
fastest way to see it working. **Option B** installs it into KiCad 9 as a real
action plugin.

---

## Requirements

- **Option A (head-less):** Python 3.8 or newer. Nothing else — no `pip install`,
  no third-party packages.
- **Option B (in KiCad):** KiCad 9.0 or newer. KiCad bundles its own Python and
  wxPython, so again nothing needs installing.

---

## Option A — run without KiCad

```bash
git clone <repository-url>
cd kicad-netlist-topology-analyzer

python -m netlist_topology_analyzer.cli examples/demo_board.json
```

Expected: a text report ending in a summary line reading
`1 error(s), 10 warning(s), 3 note(s)`. The demo board deliberately contains one
seeded defect per analyzer.

Write reports to files:

```bash
python -m netlist_topology_analyzer.cli examples/demo_board.json \
    --html report.html --json result.json --quiet
```

Open `report.html` in any browser — it is fully self-contained (inline CSS and
SVG, no network requests) and includes the topology diagram.

### Run the test suite

```bash
python -m unittest discover -s tests -t .
```

Expected: `Ran 231 tests ... OK`, in roughly 0.2 seconds. Add `-v` for the name
of every test.

### Analyse your own board

The CLI reads a board snapshot in JSON form. Produce one from a live board by
running this in KiCad's scripting console (*Tools → Scripting Console* in the PCB
editor), with the plugin installed per Option B:

```python
from netlist_topology_analyzer.core.board_extractor import extract_board
data = extract_board()
open("/path/to/board.json", "w").write(data.to_json())
```

Then analyse it anywhere, KiCad not required:

```bash
python -m netlist_topology_analyzer.cli /path/to/board.json --html report.html
```

---

## Option B — install into KiCad 9

### 1. Find KiCad's plugin folder

| OS | Path |
| --- | --- |
| Windows | `%USERPROFILE%\Documents\KiCad\9.0\3rdparty\plugins\` |
| Linux | `~/.local/share/kicad/9.0/3rdparty/plugins/` |
| macOS | `~/Documents/KiCad/9.0/3rdparty/plugins/` |

If in doubt, ask KiCad. Open the PCB editor, then *Tools → Scripting Console*:

```python
import pcbnew
print(pcbnew.PLUGIN_DIRECTORIES_SEARCH)
```

### 2. Copy the package in

Copy the **`netlist_topology_analyzer/` directory itself** (not the repository
root) into that folder. Afterwards you should have:

```
.../3rdparty/plugins/netlist_topology_analyzer/__init__.py
.../3rdparty/plugins/netlist_topology_analyzer/action_plugin.py
.../3rdparty/plugins/netlist_topology_analyzer/core/...
```

**Windows (PowerShell):**

```powershell
Copy-Item -Recurse netlist_topology_analyzer `
  "$env:USERPROFILE\Documents\KiCad\9.0\3rdparty\plugins\"
```

**Linux / macOS** — a symlink is better during development, since edits take
effect on the next *Refresh Plugins* with no re-copying:

```bash
ln -s "$(pwd)/netlist_topology_analyzer" \
      ~/.local/share/kicad/9.0/3rdparty/plugins/netlist_topology_analyzer
```

### 3. Load it

In the PCB editor: *Tools → External Plugins → Refresh Plugins*. A full restart
of KiCad also works.

### 4. Verify

The plugin should now appear as **Netlist Connectivity & Topology Analyzer**
under *Tools → External Plugins*, and as a toolbar button.

Open any board and run it. You should get a dialog with three tabs — Findings,
Metrics, and the full report.

---

## Using the dialog

**Findings tab** lists every finding with its severity, code, category and the
components or nets involved. Select a row to read the full explanation in the
detail pane below — and, with *Select part on board* ticked, to select and zoom to
the referenced footprint in the editor.

**Metrics tab** shows the board-level numbers: component and net counts, rail
census, island count, articulation points, maximum fanout, graph diameter, total
track length.

**Full report tab** is the same text the CLI prints.

**Analysis options**, with *Re-run analysis*:

- *High-fanout threshold* — raise or lower it to test whether a warning matters
  for your design.
- *Exclude power/ground from topology* — leave this **ticked**. Unticking it
  demonstrates the modelling problem it solves: ground touches nearly every part,
  so including rails makes the component graph almost complete, and articulation
  points and centrality then carry no information. On the demo board the
  projection grows from 60 edges to 199.
- *Select part on board* — cross-probing on/off.

**Save HTML report** / **Save JSON** / **Copy report** export the result. The
HTML is self-contained and safe to email.

---

## Troubleshooting

**The plugin does not appear under External Plugins.**
Check the directory nesting — `__init__.py` must sit directly inside
`.../plugins/netlist_topology_analyzer/`, one level down, not two. Then open
*Tools → Scripting Console* and run `import netlist_topology_analyzer`; any error
is printed there. Registration failures are written to stderr on purpose rather
than raised, so that a broken plugin cannot disrupt KiCad's discovery of others.

**"No board is open."**
The plugin analyses the board in the active PCB editor window. Open a `.kicad_pcb`
file first.

**Everything is reported as one big island, or no findings appear at all.**
Most likely your rails use a naming convention the patterns do not recognise, so
they are being treated as signals (or vice versa). Check the *Metrics* tab: if
*Power nets* and *Ground nets* are 0, that is the cause. Add your convention to
`ground_patterns` / `power_patterns` in `core/config.py`. There is also a fallback
that treats any net touching ≥ 60 % of components as a rail, but it only applies
on boards with 8 or more components.

**A finding looks wrong.**
Findings are heuristics, not DRC violations — the report is a review aid, not a
verdict. Connectors and test points are excluded from several checks for exactly
this reason. Every threshold is in `core/config.py`.

**An analyzer crashed.**
The run continues and the failure is printed in the report's error list rather
than being swallowed. Please report the traceback along with the board type.

**The analysis is slow on a large board.**
Betweenness centrality is O(V·E) and is skipped automatically above 400
components (`centrality_max_components` in `core/config.py`); it is omitted rather
than approximated. Nets with more than 32 pads are skipped in the projection,
since a net with N pads contributes O(N²) edges.

---

## Regenerating the demo board

```bash
python examples/make_demo_board.py
```

Rewrites `examples/demo_board.json`. Each seeded defect is documented in that
script, and `tests/test_cli.py` asserts that each one still produces its expected
finding — so the demo board doubles as a regression fixture.
