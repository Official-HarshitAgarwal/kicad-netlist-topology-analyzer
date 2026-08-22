# Plugin Proposal — Netlist Connectivity & Topology Analyzer for KiCad 9

**FOSSEE eSim Semester-Long Internship, Autumn 2026 — Submission Task 6**

---

## 1. The problem

KiCad already checks whether a board obeys the rules the designer wrote down.
DRC verifies geometry — clearances, track widths, annular rings, courtyard
overlaps — and ERC verifies schematic conventions such as unconnected pins and
conflicting pin types. Both are *local* checks: they look at a pad, a track, or a
pin pair and ask whether that item is legal.

What neither tool asks is a *global* question about the shape of the netlist as a
whole:

- Does the board form one electrical unit, or has an edit quietly split it into
  disconnected islands?
- Which single component, if it failed or were removed, would break the largest
  number of signal paths?
- Which net carries so many loads that its edge rates will suffer?
- Does every substantial IC actually have a decoupling capacitor near it, on the
  correct rail-and-ground pair?

These are the questions a reviewer asks when reading a schematic, and they are
answered today by manual inspection. They are also, structurally, graph
questions — which is precisely why they can be automated.

## 2. The proposal

I propose a KiCad 9 action plugin that treats the netlist as a graph and applies
classical graph algorithms to it, then interprets the results in electronics
terms and reports them as a ranked list of findings.

Its output is not a pass/fail verdict but a review aid: a report that says *this
component is the structural hub of your design; these two sub-circuits are not
electrically joined; this IC has no local decoupling; this bus has nine loads*,
each with an explanation of the physical consequence.

The plugin appears under *Tools → External Plugins* and on the PCB editor's
toolbar. It reads the currently open board, needs no project configuration, and
produces a scrollable dialog plus HTML and JSON exports, the HTML embedding an
inline SVG topology diagram.

## 3. Why this is both a Computer Science and an Electronics task

The task brief requires the plugin to draw on both fields. In this design they
are not merely adjacent, they are mutually dependent — neither half works alone.

**The Computer Science content is the modelling and the algorithms.** A netlist
is not a graph: it is a *hypergraph*, because one net connects an arbitrary
number of pads, not exactly two. Choosing a representation is therefore a real
design decision, and the plugin uses two:

- a **bipartite incidence graph** (component nodes ∪ net nodes) which is
  lossless and is the right structure for connectivity questions, and
- a **component projection** (each net expanded to a clique over the parts it
  touches) which is lossy but is the structure in which "which part is a
  bottleneck" is a meaningful question.

On top of those it runs union-find for connected components, Tarjan's lowlink
algorithm for articulation points and bridges, Brandes' algorithm for betweenness
centrality, and BFS for eccentricity and diameter. All are implemented from
first principles in the standard library, because KiCad ships its own embedded
Python interpreter into which installing third-party packages such as
`networkx` is awkward and cannot be assumed.

**The Electronics content is what makes the algorithmic output meaningful.** A
graph algorithm applied naively to a netlist produces confident nonsense. The
clearest example, and the design decision I am most confident about, is rail
exclusion:

> Ground touches nearly every part on a board. If GND and the power rails are
> left in the component projection, almost every part becomes adjacent to almost
> every other part. The projection approaches a complete graph, in which *no*
> vertex is an articulation point and betweenness centrality is uniform. The
> analysis silently returns "nothing to report" — not because the board is
> healthy, but because the model was wrong.

Handling that correctly requires knowing what a rail is, which is an electronics
question answered with electronics knowledge: net-name conventions (`GND`,
`AGND`, `VSS`, `VEE`, `+3V3`, `VDDA`, `VBUS`), the fact that ground patterns must
be tested *before* power patterns so that `VSS` is not swallowed by a broad
`V…` rule, hierarchical sheet-path stripping so `/power/+3V3` is recognised, and
a fanout fallback for house-style names no pattern anticipates. The same applies
throughout: reference-designator prefixes follow IEEE 315 / IEC 81346, so
mechanical parts (`H`, `MH`, `FID`) must be excluded from electrical analysis and
connectors (`J`, `TP`) must not be reported as suspicious just because they
legitimately sit at the edge of the graph. The decoupling check has to know that
a capacitor only decouples if it bridges *that IC's* rail and ground, and that
effectiveness is governed by the loop inductance — hence physical distance — and
not by capacitance value.

So: the CS supplies the machinery, the electronics supplies the semantics, and
removing either one leaves something that either cannot compute the answer or
cannot tell a real finding from an artefact.

## 4. Features in the base version

Eight independent analyzers are implemented, emitting twenty distinct finding
codes:

| Analyzer | What it reports |
| --- | --- |
| Board statistics | Component/net/pad inventory, rail census, routed length |
| Electrical islands | Disconnected sub-circuits; components with no nets at all |
| Floating & single-pad nets | Nets reaching one pad or none — incomplete connections |
| Routing completeness | Multi-pad nets with no copper |
| Signal fanout | High-fanout signal nets, escalating with load count |
| Power & decoupling | Rail inventory, ICs with no local decoupling, capacitors too far away |
| Single points of failure | Articulation points and bridges in the signal topology |
| Topology metrics | Betweenness centrality, graph diameter, structural hub ranking |

Supporting features: a results dialog with per-severity grouping, live threshold
controls, cross-probing that selects and zooms to the footprint behind a finding,
and an embedded topology diagram; HTML and JSON export, with the topology diagram
rendered as inline SVG in the HTML; a head-less CLI that runs the same engine with
no KiCad installed, for CI use and revision-to-revision diffing; and a fully
tunable configuration object holding every threshold and net-name pattern in one
auditable place.

## 5. Architecture, in one paragraph

The plugin uses a ports-and-adapters layout. Exactly one module imports
`pcbnew`; it converts the live board into plain dataclasses. Everything else —
classifier, graph model, algorithms, analyzers, report writers — is
standard-library Python that has never heard of KiCad. That single constraint
buys three things the brief asks for: the analysis engine is unit-testable on
stock CPython with no KiCad, no display server and no third-party packages; the
KiCad-facing surface is small enough to port when the SWIG API is eventually
superseded by the IPC API; and analyzers are pluggable via a registry decorator,
so a new rule is a new class and touches no existing code. The full rationale,
module breakdown and algorithm complexities are in the design document.

## 6. Verification

The base version ships 231 unit tests running in roughly a quarter of a second
under `unittest`, with no KiCad and no third-party dependency. They validate the
graph algorithms against hand-worked textbook cases, assert that a clean board
produces *no* findings (a checker that warns about everything is worthless), and
include a test that *measures* the rail-exclusion decision rather than asserting
it in prose — with rails included the demo board's projection grows from 60
edges to 199 and its articulation structure changes accordingly. Writing the
suite found and fixed two real defects in the implementation: an unreachable
rule whose filter contradicted the classifier, and a report path that silently
discarded any section an analyzer published in a non-tabular shape.

## 7. Extension roadmap for the internship period

The base version is deliberately built to be extended. Planned directions, in
rough priority order:

1. **Differential and bus awareness** — recognise `_P`/`_N` pairs and bus
   members, check length matching and symmetry of the topology between the two
   halves of a differential pair.
2. **Return-path analysis** — trace the reference plane beneath each critical
   net and flag traces crossing a plane split, which is a leading cause of
   radiated emissions.
3. **Star vs. daisy-chain topology recognition** — classify each net's routed
   shape and compare it against what the net type wants (a clock wants
   point-to-point; an I²C bus tolerates a chain; a supply wants a star).
4. **Richer cross-probing** — the base version already selects and zooms to the
   footprint behind a finding; extend this to highlight a whole flagged *net*
   and its ratsnest, and to walk a reported critical path pad by pad.
5. **Persistent configuration and rule profiles** — save thresholds per project;
   ship profiles for digital, RF and power-electronics boards.
6. **eSim / NGSPICE bridge** — reuse the extracted netlist model to emit a
   simulation-ready subcircuit for the sub-graph around a flagged component, so
   a topology finding can be followed straight into a simulation.
7. **IPC API migration** — move the extractor to KiCad's new IPC interface while
   keeping the SWIG path as a fallback, isolating the change to one module.

## 8. Deliverables

| Deliverable | Location |
| --- | --- |
| Design document | `docs/DESIGN.md` |
| Code implementation | `netlist_topology_analyzer/` |
| Instructions for execution | `README.md`, `docs/INSTALL.md` |
| Test suite | `tests/` (231 tests) |
| Worked example | `examples/demo_board.json` + generator script |
| Presentation outline | `docs/PRESENTATION.md` |

---

*Submitted for approval before implementation of further features, per the task
instructions.*
