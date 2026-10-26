# Codebase Design: Geodesic Map

## Overview

A modular, zero-backend interactive web application for calculating and visualizing geodesic great-circle flight paths on an orthographic globe projection.

## Module Seams & Interfaces

```
┌─────────────────────────────────────────────────────────────────┐
│                           UI Layer                              │
│  ┌───────────────────────────┐    ┌──────────────────────────┐  │
│  │   Search & Dropdown HUD   │    │  Flight Telemetry Panel  │  │
│  └─────────────┬─────────────┘    └────────────┬─────────────┘  │
└────────────────┼───────────────────────────────┼────────────────┘
                 ▼                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      App Controller / State                     │
│  - originCity: City | null                                      │
│  - destCity: City | null                                        │
│  - geodesicResult: GeodesicResult | null                        │
└───────────────┬───────────────────────────────┬─────────────────┘
                ▼                               ▼
┌───────────────────────────────┐ ┌───────────────────────────────┐
│        GeodesicEngine         │ │         GlobeRenderer         │
│ (Pure spherical math module)  │ │   (D3 Canvas / SVG viewport)  │
│ - computeGeodesic()           │ │ - mount()                     │
│ - 3D vector midpoint          │ │ - setRoute()                  │
│ - antipodal detection         │ │ - rotateTo() / slerp          │
│ - jet stream wind penalty     │ │ - drag & zoom controls        │
└───────────────────────────────┘ └───────────────────────────────┘
                ▲
┌───────────────┴───────────────┐
│         CityDirectory         │
│ - search(query, limit)        │
│ - loadData(citiesArray)       │
└───────────────────────────────┘
```

### 1. `GeodesicEngine` (Deep Math Module)

**Depth**: High. Conceals all 3D spherical geometry, quaternion/vector rotations, singularity handling, bearing calculations, and jet stream atmospheric heuristics behind a clean signature.

```typescript
interface LatLng {
  lat: number;
  lng: number;
}

interface GeodesicResult {
  distanceKm: number;
  distanceMiles: number;
  sphericalMidpoint: LatLng;
  isAntipodal: boolean;
  initialBearingDeg: number;
  finalBearingDeg: number;
  nominalFlightTimeHours: number;
  prevailingWindAdjustmentHours: number;
  effectiveFlightTimeHours: number;
  pathCoordinates: [number, number][]; // [lng, lat] samples along minor arc
}

function computeGeodesic(origin: LatLng, destination: LatLng): GeodesicResult;
```

**Invariants**:
- If `origin` and `destination` are identical, distance is `0` and midpoint is `origin`.
- If angular distance $> 179.5^\circ$ (`isAntipodal: true`), the geodesic path is explicitly routed across the North Pole ($lat = 90^\circ$), and `sphericalMidpoint` defaults to the North Pole.
- Longitudinal coordinates are always wrapped into $[-180, 180]$.
- Flight time assumes a baseline commercial airspeed of $900\text{ km/h}$.
- Westward flights (heading having a net negative zonal velocity component $\Delta lon \cdot \cos(lat)$) incur a prevailing wind penalty up to $+15\%$ depending on the zonal vector component.

### 2. `CityDirectory` (Data Indexing Module)

**Depth**: High. Ingests packed array representations and provides instant fuzzy/prefix search with population-rank tie-breaking.

```typescript
interface City {
  id: number;
  name: string;
  country: string;
  lat: number;
  lng: number;
}

interface CityDirectory {
  load(data: [string, string, number, number][]): void;
  search(query: string, limit?: number): City[];
  getById(id: number): City | null;
}
```

### 3. `GlobeRenderer` (Visual Viewport Module)

**Depth**: High. Manages D3 projection, TopoJSON land/graticule rendering, orthographic clipping, arc stroke styling, city pins, mouse drag inertia, scroll zoom, and spherical interpolation transitions (`d3.transition`).

```typescript
interface GlobeController {
  setRoute(geodesic: GeodesicResult | null, origin?: City | null, dest?: City | null, animate?: boolean): void;
  recenter(): void;
  resize(width: number, height: number): void;
  destroy(): void;
}

function createGlobeRenderer(container: HTMLElement, worldTopoJson: any): GlobeController;
```

### 3b. `EclipseOverlay` (Removable Layer) — `[eclipse-overlay]`

**Depth**: Shallow by design. Draws the 2 August 2027 total solar eclipse ground track (path of totality, central line, greatest-eclipse marker, and duration/local-time markers every 1000 km, shown only when zoomed in) from `data/eclipse-2027-08-02.json`, which `scripts/build_eclipse.py` derives from NASA's Besselian elements. The renderer only knows a `draw(ctx, path, projection, opts)` hook (`setEclipseOverlay`); the HUD only knows an optional `eclipseToggle` option (`visible`, `onToggle`, `getDescription` for the card shown when the switch is clicked on). Every touch point outside `src/eclipse.js` carries the `[eclipse-overlay]` tag so the feature can be deleted in one sweep (see ADR 0006).

```typescript
interface EclipseOverlay {
  draw(ctx: CanvasRenderingContext2D, path: GeoPath, projection: GeoProjection, opts?: { labels?: boolean }): void;
  setVisible(visible: boolean): void;
  isVisible(): boolean;
  isLoaded(): boolean;
  getSummary(): { greatest: { duration, localTime, utc, pathWidthKm }, markerCount } | null;
}
```

### 4. `HUD & Controls` (Interface Surface)

Custom lightweight combobox dropdowns for Origin and Destination selection, a 1-click swap button, recenter button, and a telemetry metrics card showing distance, flight time, and wind effects.
