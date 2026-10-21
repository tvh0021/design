# 5. 16k Terrain Texture

Date: 2026-10-21

## Status

Accepted. Extends the progressive WebGL terrain texture pipeline.

## Context

The globe renderer previously used a progressive two-tier terrain pipeline: a 4096x2048 base texture (`etopo.jpg`, 1.8 MB) loaded on first paint, followed by an 8192x4096 high-resolution texture (`etopo-8k.jpg`, 6.1 MB) on GPUs with `MAX_TEXTURE_SIZE >= 8192` and `deviceMemory >= 4`. At regional zoom on a desktop display the 8k texture (about 2.7 km per texel at the equator) is visibly soft.

A 16k (16384x8192) texture halves the texel size. However, a 16384x8192 RGBA texture with a full mipmap pyramid requires ~680 MB of VRAM (`16384 * 8192 * 4 * 1.333` bytes), which can trigger out-of-memory errors, tab discards, or GPU driver crashes on integrated GPUs, phones, or low-memory systems.

## Decision

We added a 16k progressive upgrade tier chained after the 8k tier, produced from NOAA source imagery and protected by multi-level hardware and interaction gates:

- **Source Asset**: NOAA ETOPO1 Global Relief Model (ice surface, 1 arc-minute grid, color relief TIFF at 21600x10800, ~700 MB uncompressed).
- **Processing Pipeline**: Resized using ImageMagick 7 with Lanczos filtering, metadata stripped, 2x2 chroma subsampling, non-interlaced, and tuned to quality 70 to fit within a 24 MB budget:
  ```bash
  magick -limit memory 6GiB -limit map 12GiB color_etopo1_ice_full.tif \
    -filter Lanczos -resize 16384x8192! -strip -sampling-factor 2x2 \
    -interlace none -quality 70 maps/data/etopo-16k.jpg
  ```
  The resulting asset `maps/data/etopo-16k.jpg` is 24,177,927 bytes (23.06 MiB / 24.18 MB) at exactly 16384x8192 pixels.
- **Progressive Chain**: The renderer loads the 4k base texture first, then evaluates `canUseHiRes()` to load the 8k texture. After the 8k upload succeeds, it evaluates `canUseUltra()` before requesting the 16k texture.
- **Hardware & Environment Gating (`canUseUltra`)**:
  - `gl.getParameter(gl.MAX_TEXTURE_SIZE) >= 16384`: Ensures GPU hardware supports 16k textures.
  - `navigator.deviceMemory === undefined || navigator.deviceMemory >= 8`: Requires at least 8 GB of host RAM (or undefined where unsupported).
  - `!window.matchMedia('(hover: none)').matches`: Excludes primary touch/mobile devices to protect battery and mobile GPU memory budgets.
  - Option `terrainUltraUrl` defaults to `'data/etopo-16k.jpg'`; passing `terrainUltraUrl: null` disables the tier.
- **Robust OOM Fallback**: `uploadTexture` queries `gl.getError()` immediately after `texImage2D`. If `gl.OUT_OF_MEMORY` is reported, it catches the error, re-uploads the previous successful lower-resolution image (restoring clean WebGL state), and returns `false`, halting the upgrade chain.
- **No anisotropic filtering at 16k**: with the 4x anisotropy used for the 4k and 8k textures, the 16k texture rendered at 1.5 fps on an M1 Max under Chrome's ANGLE Metal backend (144 fps at 8k), and an isolated benchmark showed a single anisotropic draw of the 16k texture never completing while trilinear mipmapped draws ran at full speed. The 16k upload therefore resets anisotropy to 1 and relies on mipmaps alone.
- **API**: Exposed `isUltra: () => ultraActive` on the WebGL terrain instance alongside `isHiRes`, and `getTerrainTier()` ('none' | 'base' | 'hires' | 'ultra') on the renderer; with `?debug=1` the tier is mirrored to `data-terrain-tier` on the document element. The `?texture=4k|8k` URL parameter caps the tier for comparison.

## Consequences

- **Positive**: High-resolution desktop displays receive crisp, detailed global terrain relief without compromising initial load speed.
- **Negative**: Adds a ~23.1 MiB JPEG asset to the static repository assets and allocates ~680 MB of GPU memory on qualifying desktop machines.
- **Trade-off**: Mobile devices and lower-memory systems remain capped at 4k or 8k, preserving stability while delivering maximum fidelity to capable workstations.
