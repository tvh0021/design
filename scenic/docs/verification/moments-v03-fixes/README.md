# Moments v0.3 fixes, 2026-11-03

Agent reports from the fix round, kept for the changed-label lists. The
rules that shipped are in `docs/SCORE_SPEC.md` and the README; where these
reports disagree with them, the spec wins.

- `sol-moment-fixes.md`: naming by geometry, the Thousand Islands
  long-bridge rule, Erie Canal name propagation, bridge-anchored clusters.
  A review then found the naming rule over-corrected (creek over a lake
  within 400 m), bridge labels taking the road's name, and bridge anchoring
  lowering moment values; those were fixed afterwards, so about 27 of the
  labels listed here changed again.
- `astra-boundary.md`: the first boundary mask, from the Census cartographic
  file clipped at the shoreline. It was replaced by the TIGER/Line legal
  boundary unioned over New York and its five US neighbours, with a rule
  that keeps a stop whose own road edge touches the mask, so border bridges
  survive and only the Canadian side is dropped (24 moments).
