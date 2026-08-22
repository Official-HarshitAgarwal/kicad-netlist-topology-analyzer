# Presentation / Demo Outline (5–10 minutes)

The optional deliverable. Structure below is timed for 8 minutes; cut §6 and
shorten §3 to land at 5.

The single idea to leave the audience with: **the graph algorithms are the easy
half — the electronics knowledge is what stops them producing confident
nonsense.** Everything else supports that.

---

## 1. The gap (60 s)

Open KiCad with the demo board. Run DRC — clean, or nearly so.

Say: DRC and ERC are *local* checks. They ask whether this pad, this track, this
pin pair is legal. Nobody asks whether the board still forms one electrical unit,
or which component the whole design hangs off. Those questions get answered by a
human reading the schematic.

## 2. The plugin, live (90 s)

*Tools → External Plugins → Netlist Connectivity & Topology Analyzer.*

Walk the Findings tab top-down, one line each:

- **NTA-101** — D2 is on the board with no pads on any net. Invisible to DRC.
- **NTA-100** — the netlist is in 3 electrical islands. Legitimate for isolated
  designs; otherwise a missing connection.
- **NTA-144** — U4 has no decoupling capacitor. Note that its rail is
  `+3V3_MEM`, ferrite-filtered off `+3V3`: capacitors on `+3V3` do not decouple
  it. That is the check knowing something about the circuit, not just counting.
- **NTA-150 / NTA-160** — U1 is an articulation point and the centrality hub.

Click a finding: it selects and zooms to the footprint in the editor.

## 3. Why a netlist is not a graph (2 min)

The slide that carries the talk.

A net connects an arbitrary number of pads, so a netlist is a **hypergraph**.
There is no such thing as "the graph of the netlist" — you have to choose, and the
choice decides which questions you can answer. Show both:

- **Bipartite incidence graph** (components ∪ nets) — lossless, right for
  connectivity.
- **Component projection** (each net → a clique) — lossy, but the only place
  where "which *component* is a bottleneck" is a meaningful question.

## 4. The decision the whole thing rests on (2 min)

**Ground touches nearly every part on the board.**

So if you leave rails in the projection, every part becomes adjacent to nearly
every other. The projection approaches a complete graph — and in a complete graph
no vertex is an articulation point, centrality is uniform, and the diameter is 1.

The analysis then reports *nothing interesting*. Not because the board is sound —
because the model was wrong. **Silence from a modelling error is the worst failure
mode a checker can have, because it is indistinguishable from success.**

Demonstrate it live: untick *Exclude power/ground from topology*, hit *Re-run*.
Watch the bridge count drop from 6 to 4 and the hub's centrality shift. Then show
the numbers on the seven-part fixture board where the effect is starker: 6
projection edges and one articulation point becomes 19 edges and **zero**.

Then land the point: knowing what a rail *is* — `VSS`, `VEE`, `AGND`, `+3V3`,
`VDDA`, `/power/+3V3` — is electronics knowledge. Ground patterns have to be
tested before power patterns or `VSS` gets swallowed by a broad `V…` rule. This
is where the two halves of the task meet.

## 5. Architecture in one slide (60 s)

One rule: **exactly one module imports `pcbnew`.**

Consequences: 231 tests run on stock CPython with no KiCad and no display server
in 0.2 s; migrating to KiCad's IPC API means rewriting 450 of 4,440 lines; a new
rule is a `@register`ed class that no existing file has to know about.

Show the CLI running the same engine head-less, with `--fail-on error` returning
exit code 1 — the analysis can gate a build.

## 6. What testing found (45 s — cut if short on time)

Writing the suite found two real defects: a rule that could never fire because
its filter contradicted the classifier, and a report path that silently discarded
any section published in an unexpected shape. Both are the kind of bug only tests
find. Mention the 5,000-node test that forced the Tarjan implementation to be
iterative rather than recursive.

## 7. Where it goes next (45 s)

Differential pairs and buses; return-path and plane-split analysis; topology-shape
recognition (a clock wants point-to-point, an I²C bus tolerates a chain, a supply
wants a star); and an eSim/NGSPICE bridge that emits a simulation-ready subcircuit
for the sub-graph around a flagged component — so a topology finding can be
followed straight into a simulation.

---

## Recording notes

- Record at 1920×1080; KiCad's dialogs are dense and shrink badly.
- Have the demo board open *before* recording, and the plugin already refreshed.
- Rehearse the untick-and-re-run moment — it is the most persuasive fifteen
  seconds in the demo and it is easy to fumble.
- Keep the CLI in a terminal already `cd`'d into the repository.
- Screen-record only; a webcam inset adds nothing here.

## Slide checklist

1. Title — plugin name, task, your name
2. DRC/ERC are local checks; these questions are global
3. Live demo (no slide)
4. Netlist as hypergraph → two representations, side by side
5. Rails destroy the projection — with the before/after numbers
6. Architecture: one module imports `pcbnew`
7. Eight analyzers, twenty findings — the table from the README
8. Roadmap
