# Design Document — KiCad Netlist Connectivity & Topology Analyzer

**FOSSEE eSim Semester-Long Internship, Autumn 2026 — Submission Task 6**
Target: KiCad 9.0 · Plugin version 0.1.0 · License MIT

---

## 1. Purpose and scope

KiCad's DRC and ERC are *local* checks: they examine one pad, track or pin pair
and ask whether it is legal. This plugin answers *global* questions about the
netlist's structure instead — is the board one electrical unit, which component
is the structural bottleneck, which nets are overloaded, which ICs lack local
decoupling. Those are graph questions, so they can be computed; but a graph
algorithm applied naively to a netlist produces confident nonsense, so the
electronics domain rules that make the results meaningful are a first-class part
of the design rather than an afterthought.

The deliverable is a working base version: eight analyzers, twenty finding codes,
a wxPython results dialog inside KiCad, HTML and JSON export (the HTML embedding
an inline SVG topology diagram), a head-less CLI, and 231 unit tests.

## 2. Architecture

### 2.1 The organising constraint

The whole design follows from one rule:

> **Exactly one module imports `pcbnew`.**

That module (`core/board_extractor.py`) converts the live KiCad board into plain
dataclasses. Every other module — classifier, graph model, algorithms,
analyzers, report writers, CLI — is standard-library Python that has never heard
of KiCad. This is the ports-and-adapters (hexagonal) pattern: KiCad is an
*adapter* plugged into a KiCad-agnostic core, not an ambient dependency.

Three concrete benefits, each of which the task brief asks for:

| Benefit | Consequence |
| --- | --- |
| Testability | 231 tests run on stock CPython — no KiCad, no display server, no third-party packages, well under a second |
| Portability | When KiCad's SWIG API gives way to the IPC API, one 450-line module changes and nothing else does |
| Extensibility | A new analysis rule is a new class in one file and touches no existing code |

### 2.2 Layer diagram

```
                    ┌──────────────────────────────────────────┐
  KiCad 9 PCB       │  action_plugin.py                        │  ← only entry
  editor ──────────▶│  pcbnew.ActionPlugin subclass            │    point KiCad
  Tools ▸ External  │  defaults() / Run()                      │    knows about
  Plugins           └────────────────┬─────────────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
┌──────────────────┐   ┌───────────────────────────┐   ┌────────────────────┐
│ KiCad adapter    │   │ Analysis core             │   │ Presentation       │
│                  │   │  (stdlib only)            │   │                    │
│ board_extractor  │──▶│ classifier                │──▶│ ui/results_dialog  │
│  imports pcbnew  │   │ graph_model               │   │  (wxPython)        │
│  → BoardData     │   │ algorithms                │   │ report.py          │
│                  │   │ analyzers (registry)      │   │  text/JSON/HTML/SVG│
│                  │   │ engine  ◀── config        │   │ cli.py (head-less) │
└──────────────────┘   └───────────────────────────┘   └────────────────────┘
```

The two dependency directions that matter: the core never imports upward
(it cannot reach the adapter or the UI), and the UI/CLI never reach around the
engine — both call `analyze_board()` and nothing else, which is why they cannot
diverge in behaviour.

### 2.3 Module breakdown

| Module | Lines | Responsibility |
| --- | --- | --- |
| `__init__.py` | 39 | Registers the plugin as an import side effect, inside a broad `try`/`except` so a failure cannot disrupt KiCad's scan of *other* plugins |
| `action_plugin.py` | 113 | `pcbnew.ActionPlugin` subclass: `defaults()` describes the plugin, `Run()` wires extractor → engine → dialog and turns any failure into a message box |
| `core/board_extractor.py` | 450 | The only `pcbnew` importer. Walks footprints, pads, nets and tracks; converts internal units to mm; degrades gracefully across API spellings |
| `core/model.py` | 451 | `Pad`, `Component`, `Net`, `TrackSegment`, `BoardData`, `Finding`, `AnalysisResult` — plain dataclasses with JSON round-tripping |
| `core/config.py` | 220 | Every threshold and net-name pattern in one auditable place, with compiled-regex caching |
| `core/classifier.py` | 96 | Assigns `POWER` / `GROUND` / `SIGNAL` / `UNCONNECTED` roles to nets |
| `core/graph_model.py` | 218 | Builds the bipartite incidence graph and the rail-free component projection |
| `core/algorithms.py` | 403 | Union-find, Tarjan lowlink, Brandes betweenness, BFS — from first principles |
| `core/analyzers.py` | 961 | The eight rules, each a registered `Analyzer` subclass |
| `core/engine.py` | 112 | Orchestrates one run; isolates each analyzer's failures |
| `core/report.py` | 578 | Text, JSON, self-contained HTML and SVG writers |
| `ui/results_dialog.py` | 473 | wxPython dialog: tabbed findings/metrics/report, live thresholds, cross-probing, export |
| `cli.py` | 195 | Head-less runner with CI-friendly exit codes |

Total: ~4,440 lines of implementation (the table above omits three small files —
the two package `__init__` modules and the icon generator) and ~2,120 lines of
tests.

## 3. Data model

`BoardData` is the boundary type — the neutral representation the adapter
produces and the core consumes. It is deliberately dumb: dataclasses, primitive
fields, no behaviour that depends on KiCad.

```
BoardData
├── name, meta{kicad_version, …}
├── components: [Component]
│     reference, value, footprint, x, y, layer, dnp, excluded_from_bom
│     └── pads: [Pad]  reference, number, net_name, net_code, x, y
├── nets: [Net]
│     name, code, role, track_length_mm, track_count, via_count
│     └── pads: [Pad]   ← the *same objects* as in Component.pads
└── tracks: [TrackSegment]  net_code, net_name, width_mm, length_mm, layer, is_via
```

Two details are load-bearing:

**Pad objects are shared, not copied.** A `Pad` appears in exactly one
`Component.pads` list and one `Net.pads` list, and both references point at the
same object. That is what makes the netlist traversable in both directions
without a lookup table, and a unit test asserts it with `assertIs` after a full
JSON round-trip.

**The model round-trips through JSON losslessly.** `to_dict()`/`from_dict()`
preserve connectivity, roles and routing. This is what lets the CLI analyse a
snapshot with no KiCad present, lets the test suite use hand-written fixture
boards, and lets two board revisions be diffed.

Findings are uniform: `code` (e.g. `NTA-144`), `title`, `severity`
(`ERROR`/`WARNING`/`INFO`), `category`, `detail` (a full prose explanation of the
*physical* consequence), `refs` (components or nets to cross-probe to), and an
optional numeric `value`.

## 4. Graph modelling — the central design decision

### 4.1 A netlist is a hypergraph, not a graph

One net connects an arbitrary number of pads, not exactly two. So there is no
single "the graph of the netlist"; a representation must be chosen, and the
choice determines which questions are answerable. The plugin builds two.

**Bipartite incidence graph** — nodes are components ∪ nets, edges are pad
memberships. Lossless: no information about which parts share which net is
destroyed.

```
   U1 ──── SDA ──── U3
    │       │
    │       └────── U6
    └─── SPI_MISO ── U4
```

Used for connectivity questions (islands, floating nets), where losing the
distinction between "these three parts share one net" and "these three parts are
pairwise connected" would give wrong answers.

**Component projection** — each net is expanded into a clique over the parts it
touches, and net nodes disappear.

```
   U1 ──── U3            U1 ──── U4
     ╲    ╱               (from SPI_MISO)
      ╲  ╱
       U6      (SDA becomes a triangle U1–U3–U6)
```

Lossy — a 3-pad net and three 2-pad nets look identical afterwards — but it is
the only representation in which "which *component* is a bottleneck" is even a
meaningful question, because bottleneck-ness is a property of vertices and in the
bipartite graph half the vertices are nets.

### 4.2 Why rails must be excluded from the projection

This is the design decision the whole topology analysis rests on.

Ground touches nearly every part on a board. If GND and the power rails are left
in the projection, every part that touches GND becomes adjacent to every other
part that touches GND — that is, almost all of them. The projection approaches a
complete graph. In a complete graph:

- no vertex is an articulation point (removing any one leaves the rest connected),
- betweenness centrality is uniform (every pair is already adjacent, so no
  shortest path passes *through* anything),
- the diameter collapses to 1.

The analysis then reports "nothing structurally interesting" — not because the
board is sound, but because the model was wrong. **Silence caused by a modelling
error is the worst possible failure mode for a checker,** because it is
indistinguishable from success.

Measured on the demo board (`tests/test_cli.py`, `tests/test_graph_model.py`):

| | Rails excluded | Rails included |
| --- | --- | --- |
| Projection edges | 60 | 199 |
| Connections with no redundant path (bridges) | 6 | 4 |
| U1 betweenness centrality | 0.2677 | 0.3705 |

On the seven-part fixture board the effect is starker still: 6 projection edges
and one articulation point (U1) with rails excluded, versus 19 edges and *zero*
articulation points with them included. Tests assert the articulation structure
exactly and the edge growth as a ratio, so the design decision is measured
rather than asserted in prose. The GUI ships a checkbox and the CLI a
`--include-rails` flag so a user can reproduce the degradation themselves.

Excluding rails correctly is an *electronics* problem, addressed in §5.

### 4.3 Fanout guard

A net with N pads contributes O(N²) projected edges. A 40-pad ground-like bus
would therefore dominate both runtime and the resulting structure while adding no
insight, so nets above `projection_max_net_fanout` (default 32) are skipped in
the projection — they are still fully analysed everywhere else.

## 5. Electronics domain rules

The electronics knowledge is concentrated in `config.py` (declarative patterns
and thresholds) and `classifier.py` (the decision procedure), so it is auditable
in one place instead of scattered through algorithm code.

**Rail recognition.** Patterns follow real naming conventions — for ground,
`GND`, `AGND`, `DGND`, `PGND`, `VSS`, `VSSA`, `0V`, `EARTH`, `CHASSIS`, `VEE` and
anything containing `GROUND`; for power, `VCC`, `VCCIO`, `VDD`, `VDDA`, `AVDD`,
`DVDD`, `VBUS`, `VBAT`, `VIN`, `VOUT`, `VREF`, `POWER`, `PWR`, `RAIL`, and
numeric forms such as `+3V3`, `3V3`, `+5V`, `-12V`, `V3V3`, `P3V3` and
`+3V3_MCU`. The authoritative list is `DEFAULT_GROUND_PATTERNS` and
`DEFAULT_POWER_PATTERNS` in `config.py`.

Three subtleties, each with a test:

1. **Ground is tested before power.** `VSS`, `VSSA` and `VEE` would otherwise be
   swallowed by the broad `V…` power patterns and misclassified.
2. **Hierarchical names are stripped to their leaf.** KiCad prefixes nets inside
   a hierarchical sheet with the sheet path, so `/power_supply/+3V3` must be
   recognised as `+3V3` — but auto-generated names like `Net-(U1-Pad3)` are left
   untouched.
3. **A fanout fallback catches house styles.** A net touching ≥ 60 % of all
   components is treated as a rail even if no pattern matches, which catches
   names like `MAIN_SUPPLY`. This is only trusted on boards with ≥ 8 components,
   because on a three-part board every net looks like a rail. Mechanical and DNP
   parts are excluded from the denominator — twenty mounting holes would
   otherwise push a real rail below the threshold.

**Reference-designator classes** follow IEEE 315 / IEC 81346, with the prefix
extracted from the letters before the digits (`R10A` → `R`, `u5` → `U`,
`LOGO` → `LOGO`):

| Class | Prefixes | Why the analysis cares |
| --- | --- | --- |
| Capacitor | `C`, `CAP` | Candidates for decoupling |
| IC / active | `U`, `IC`, `Q`, `AR`, `RN` | Need decoupling; likely hubs |
| Connector / test point | `J`, `P`, `CN`, `CON`, `TP`, `X`, `SW`, `K` | Legitimately low-fanout and legitimately at graph edges — must not be reported as suspicious |
| Mechanical | `H`, `MH`, `MK`, `FID`, `LOGO`, `G` | No electrical meaning; excluded entirely |

**Fanout physics.** Each additional load on an unterminated net adds input
capacitance and a trace stub. Both degrade edge rates, and stubs cause
reflections. So the finding escalates: above 8 pads a warning, above 16 an
error — and rails are never flagged, since a supply is *supposed* to reach
everything.

**Decoupling physics.** A capacitor only decouples an IC if it bridges *that
IC's own* power net and *that IC's own* ground net; a capacitor on a different
rail is irrelevant no matter how close it sits. This matters on boards with
ferrite-filtered rails, where an IC on `+3V3_MEM` is not decoupled by a capacitor
on `+3V3` — the demo board seeds exactly that case (finding NTA-144 on U4). And
effectiveness is set by the **loop inductance** between capacitor and IC, so
distance matters more than capacitance value; hence the separate advisory finding
NTA-145 when the nearest qualifying capacitor is beyond 10 mm. Only ICs with ≥ 6
pads are checked, to avoid flagging every three-pin regulator.

## 6. Algorithms

All implemented from first principles in `core/algorithms.py`. `networkx` would
have been convenient, but KiCad ships an embedded Python interpreter into which
installing third-party packages is awkward and cannot be assumed on a user's
machine — so a stdlib-only implementation is a deployment requirement, not a
purity exercise.

| Algorithm | Complexity | Used for |
| --- | --- | --- |
| Union-find (union by rank + path compression) | O(E·α(V)) | Electrical islands (NTA-100) |
| Tarjan lowlink, **iterative** | O(V + E) | Articulation points + bridges (NTA-150, 151, 152) |
| Brandes betweenness centrality | O(V·E) | Structural hub ranking (NTA-160); NTA-161 when skipped |
| BFS | O(V + E) | Distances, eccentricity, and the `signal_graph_diameter` metric |
| Bucketed top-N | O(V log V) | Ranked report sections |

Two implementation notes worth recording:

**The lowlink DFS is iterative, with an explicit stack.** A textbook recursive
Tarjan hits CPython's ~1000-frame recursion limit on a long chain of parts —
which is not a hypothetical shape for a netlist. A test builds a 5,000-node path
and asserts 4,998 articulation points; the recursive version would raise
`RecursionError` long before finishing.

**One traversal yields both articulation points and bridges.** They are different
readings of the same discovery/lowlink bookkeeping (`low[child] > disc[node]` for
a bridge, `low[child] >= disc[node]` plus root handling for a cut vertex), so
computing them separately would traverse the graph twice for no reason.

Betweenness centrality is skipped above 400 components (`centrality_max_components`),
because O(V·E) on a large board would stall the GUI thread. The metric is simply
omitted rather than approximated — reporting a wrong number is worse than
reporting none.

## 7. Analyzers and findings

Each rule is a subclass of `Analyzer` registered by decorator:

```python
@register
class FanoutAnalyzer(Analyzer):
    name = "fanout"
    title = "Signal fanout"

    def analyze(self, ctx):           # ctx: board, graph, config, result
        ...
        yield Finding(code="NTA-130", ...)
```

`REGISTRY` plus `@register` is the Open/Closed principle made concrete: adding a
rule means adding a class, and `engine.py`, `report.py` and the dialog all pick
it up with no edits. The report renderer derives table headers from the keys of
whatever dicts an analyzer publishes, so a new rule's section appears in text,
HTML and JSON without touching the writers — a test proves this by publishing a
section the codebase has never seen.

| Analyzer | Codes | Reports |
| --- | --- | --- |
| `statistics` | — | Component/net/pad inventory, rail census, routed length |
| `islands` | NTA-100, 101 | Disconnected sub-circuits; components with no nets at all |
| `floating_nets` | NTA-110, 111, 112 | Nets reaching one pad or none |
| `unrouted` | NTA-120, 121, 122 | Multi-pad nets with no copper |
| `fanout` | NTA-130 | High-fanout signal nets, escalating with load count |
| `power` | NTA-140–145 | Rail inventory; ICs with no local decoupling; capacitors too far away |
| `spof` | NTA-150, 151, 152 | Articulation points and bridges in the signal topology |
| `topology_metrics` | NTA-160, 161 | Betweenness centrality, graph diameter, hub ranking (NTA-161 reports when centrality is skipped on a large board) |

Two behaviours keep reports usable. **Findings are capped per rule**
(`max_findings_per_rule`, default 12): beyond the cap a rule emits the first N
plus one summary finding covering the rest, because a report that buries the
reader in near-identical warnings gets skimmed and ignored, which defeats the
point. And **a clean board produces no findings at all** — asserted by negative
tests, since a checker that warns about everything is worthless.

## 8. How the layers interact

### 8.1 The pipeline

```
BoardData
   │
   ▼  [1] classify_board(board, cfg)      → POWER / GROUND / SIGNAL / UNCONNECTED
   │
   ▼  [2] NetlistGraph(board, cfg)        → bipartite incidence + rail-free projection
   │
   ▼  [3] for each analyzer: analyze(ctx) → Findings + metrics + sections
   │
   ▼
AnalysisResult ──▶ ResultsDialog / render_text / render_json / render_html / render_svg
```

The ordering is a real dependency chain, not an arbitrary sequence: classification
must precede graph construction because the projection needs to know which nets
are rails, and the analyzers need both.

### 8.2 Failure isolation

Each analyzer runs inside its own `try`/`except`. A rule that trips over an
unusual board construct appends an error string to `AnalysisResult.errors` and
the run continues, so the user still gets every other finding. The failure is
*surfaced in the report*, never swallowed — a test registers a deliberately
exploding analyzer and asserts both that the run completes with other findings
intact and that "boom" appears in the errors list.

The same defensive posture applies at the KiCad boundary. Registration in
`__init__.py` is wrapped, because an exception escaping a plugin's `__init__`
during KiCad's directory scan can disrupt discovery of *other* plugins. `Run()`
imports the analysis modules lazily, so a syntax error in the core produces a
readable dialog rather than a silently missing menu entry.

### 8.3 KiCad API interaction

`board_extractor.py` walks `board.GetFootprints()`, each footprint's `Pads()`,
`board.GetNetsByName()` and `board.GetTracks()`, converting KiCad's internal
units to millimetres via `pcbnew.ToMM`.

It is written defensively on purpose: helper functions try several method
spellings (`_try_call(footprint, ("IsDNP",))` with a separate `GetAttributes()`
fallback, `_to_mm` trying both `ToMM` and `Iu2Millimeter`) and fall back rather
than raise, because these bindings have been renamed repeatedly across KiCad 6→9
and a plugin that hard-fails on one renamed getter is worthless the day after an
upgrade. Anything unreadable is recorded as a warning in the extraction result
instead of aborting the run. Declared nets with no pads attached are captured
deliberately — a named net with no members is usually a leftover from an edit,
and reporting it is the point of NTA-111.

### 8.4 UI

`ui/results_dialog.py` is a wxPython dialog (wxPython is what KiCad itself uses,
so no new dependency) with three tabs — Findings (sortable list plus a detail
pane), Metrics, and the full text report in a monospace control. It offers live
threshold controls with a **Re-run analysis** button, so a user can test whether
a fanout warning is real by moving the threshold, and can untick "exclude
power/ground from topology" to watch the structure collapse. Selecting a finding
cross-probes: the referenced footprint is selected and zoomed to in the PCB
editor, best-effort and silent on failure, because a popup on every selection
would be more annoying than a feature quietly not working.

### 8.5 Automation

`cli.py` runs the identical engine head-less:

```
python -m netlist_topology_analyzer.cli board.json --html report.html
python -m netlist_topology_analyzer.cli board.json --fail-on warning
```

Exit codes are the automation contract: 0 success, 1 findings at or above
`--fail-on`, 2 usage error. JSON output is deterministic (stable key order, the
timing field being the only varying value), which is what makes diffing two
board revisions meaningful. HTML reports are fully self-contained — inline CSS,
inline SVG, no `<script>`, no `<link>`, no `src=` — so they open offline and can
be attached to a review email; a test asserts that self-containment rather than
trusting it.

## 9. Verification

231 tests across seven modules, `python -m unittest discover -s tests -t .`,
well under a second, no KiCad, no display server, no third-party packages.

| Module | Covers |
| --- | --- |
| `test_algorithms.py` | Graph algorithms against hand-worked textbook cases; the 5,000-node recursion test |
| `test_model.py` | Dataclass behaviour, prefix extraction, JSON round-trip with pad-object identity |
| `test_classifier.py` | ~40 real net names; pattern ordering; fanout fallback; DNP/mechanical exclusion |
| `test_graph_model.py` | Both representations; the rail-exclusion measurement |
| `test_analyzers.py` | Each rule, positive and negative; finding caps; analyzer failure isolation |
| `test_report.py` | All four writers; determinism; HTML escaping; unknown-section rendering |
| `test_cli.py` | Argument parsing, exit codes, written files; demo board as regression fixture |

Design choices in the suite worth noting. **Negative assertions are as important
as positive ones** — a clean board must produce no errors, no spurious
connectivity findings, and no crashed analyzers. **The demo board is a regression
fixture**, not just a demo: `examples/make_demo_board.py` seeds one specific
defect per analyzer and documents which finding each should produce, and tests
assert that mapping holds (NTA-101 → D2, NTA-144 → U4, NTA-145 → U8, …).
**HTML escaping is tested** because net names legitimately contain `<`, `>` and
`&`.

Writing the suite found two real defects in code written before it:

1. **NTA-111 was unreachable.** `FloatingNetAnalyzer` filtered pad-less nets on
   `role != UNCONNECTED`, but the classifier assigns exactly that role to every
   pad-less net — so the rule could never fire. The extractor's own comments
   confirmed the rule was intended to work. Fixed by removing the contradictory
   gate and documenting why the filter must not be reinstated.
2. **Non-tabular report sections were silently discarded.** `_as_rows()` returns
   `None` for a payload that is not a list of uniform dicts, and both the text
   and HTML renderers skipped those sections — so a future analyzer publishing a
   string or scalar would have vanished without trace. Fixed with an `_as_text()`
   fallback so the report renders whatever it is handed.

Both are the kind of defect only a test suite finds, which is the point.

## 10. Extensibility

| Extension | What it takes |
| --- | --- |
| New analysis rule | One `@register`ed class; report and GUI pick it up automatically |
| New threshold or naming convention | One field or pattern in `config.py` |
| New output format | One function in `report.py` consuming `AnalysisResult` |
| House-style rail names | Override `power_patterns` / `ground_patterns` |
| **Migration to KiCad's IPC API** | Rewrite `board_extractor.py` only — 450 of ~4,440 lines, with 231 tests still passing unchanged, since nothing else knows KiCad exists |

That last row is the payoff of the §2.1 constraint, and the reason the constraint
was worth accepting in the first place.

Planned during the internship: differential-pair and bus awareness; return-path
and plane-split analysis; net topology-shape recognition (star vs. daisy chain vs.
what the net type wants); richer cross-probing that highlights whole nets and
walks critical paths; persistent per-project rule profiles for digital, RF and
power boards; and an eSim/NGSPICE bridge that emits a simulation-ready subcircuit
for the sub-graph around a flagged component, so a topology finding can be
followed straight into a simulation.

---

*See `README.md` and `docs/INSTALL.md` for installation and execution
instructions, and `docs/PROPOSAL.md` for the original proposal.*
