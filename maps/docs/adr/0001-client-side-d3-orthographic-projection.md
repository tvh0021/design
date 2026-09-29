# 1. Pure Client-Side D3 Orthographic Projection

Date: 2026-09-29

## Status

Accepted

## Context

The previous visualization used Matplotlib with Basemap (`great_circle.py`), which required Python and generated static PNG images. The new tool must run interactively in modern web browsers, hosted as a static site on GitHub Pages (`github.io`), and be embeddable within a wider portfolio site without any active backend server.

Options considered:
1. Server-side rendering (Python/Flask/FastAPI + Matplotlib/Cartopy delivering images or tiles)
2. Heavy WebGL/Three.js 3D earth
3. Client-side vector projection via D3.js (`d3-geo` + `topojson-client`) rendered to HTML5 Canvas / SVG

## Decision

We will build the globe as a pure client-side application using D3.js (`d3-geo` and `topojson-client`) with a standard low-resolution World Atlas (110m, ~120 KB TopoJSON) rendered to an HTML5 Canvas / SVG viewport.

## Consequences

- **Positive**: Zero backend hosting cost, instant deployment on GitHub Pages, full offline capability after initial load, high frame-rate interactive drag-rotation and smooth transition interpolation.
- **Negative**: Visual fidelity is constrained by vector geometry resolution; client device GPU/CPU must handle projection math on drag.
- **Trade-off**: World Atlas 110m is lightweight enough to run smoothly even on mobile browsers while providing accurate continental and country outlines.
