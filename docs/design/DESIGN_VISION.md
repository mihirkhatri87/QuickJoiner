# The Orrery — UX North Star

Author: UX. Prototype: `orrery-prototype.html` (self-contained; open in any browser).
Maps to PRD W10. 2026-07-11.

## The idea in one sentence

Stop rendering the assistant as a chat log; render **the state of the org's knowledge as
navigable territory** — luminous where learned, visibly dark where not — and make language
a thin instrument (the *intent line*) over that territory.

## Why this and not another chat skin

Every differentiator QuickJoiner owns is *spatial or structural*, and chat hides all of it:
- **Refusal** becomes *dark zones* — uncharted territory with a dashed frontier and pulsing
  unknowns. Honesty stops being an apologetic sentence and becomes geography you can see
  shrink as sources connect. (No competitor can render this screen truthfully.)
- **Evidence** becomes *threads*: a probe flies from your question to the relevant
  territory, gold threads link the dossier's claims to their exact evidence nodes; hovering
  a claim re-lights its thread. Provenance you can watch.
- **The graph** stops being a side feature; it is the ground plane.
- **Ramp** becomes an *orbit ribbon* — day 28 of 90, milestones as waypoints — the
  manager's product (PRD W1) and the joiner's motivation in one strip.
- **Answers** become *dossiers*: pinned, cited artifacts docked at the map's edge — not
  scrollback that evaporates.

## Grammar of the interface

| Element | Meaning | Token |
|---|---|---|
| Luminous node | learned entity (repo/service teal, package gold, ticket/person violet) | product tokens |
| Faint edge / gold dashed thread | recorded relationship / evidence trail for the current dossier | gold = provenance |
| Dark zone + dashed frontier | not learned; refusal heat (12 teammates hit this) | violet = unknown |
| Probe | a question in flight; teal when it lands in light, violet flare when it hits dark | |
| Dossier | cited answer artifact; verdict stamp = grounded / not-learned | |
| Remediation chips | one-click gap fixes: connect / ask a human / teach | PRD W2.3, W5.2 |

## Deliberate choices

- **Single-theme world** (dark cartographic instrument): committed, not defaulted — the
  fog-of-war metaphor requires darkness to mean something. The shipped app keeps its
  dual-theme evidence-ledger UI; the Orrery is the exploration/coverage *mode*, reachable
  from it.
- **Mono-dominant type** (instrument readouts) with a spaced geometric display only for the
  wordmark — data is the aesthetic.
- **Motion is meaning only**: probe flight, thread draw, frontier pulse. Nothing else moves.
  `prefers-reduced-motion` collapses all of it to instant states.

## Productization path (from fiction to feature)

1. **v0 (prototype, done)**: canned probes, simulated territory.
2. **v1**: fog layer + refusal heat on the existing GraphView (real /api/graph + gap
   backlog data); dossier = existing answer card docked on the canvas.
3. **v2**: intent line replaces composer in map mode; evidence threads driven by real
   citation→chunk→doc joins; remediation chips wired to /connect and people graph.
4. **v3**: ramp ribbon fed by analytics (W1); territory diffing ("what lit up this week").
