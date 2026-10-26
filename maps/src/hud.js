/**
 * Geodesic Map - Modern Interactive Flight HUD & Controls
 *
 * Glassmorphic HUD overlay featuring:
 * - Origin & Destination searchable autocomplete comboboxes (top 10k cities index)
 * - 1-Click Swap Cities button
 * - Recenter Globe / Map button
 * - Projection Toggle (3D Orthographic Globe vs 2D Robinson flat map)
 * - Real-time flight telemetry cards:
 *   * Geodesic Distance (km & nautical/statute miles)
 *   * Estimated Nonstop Flight Time (calibrated jet stream headwinds/tailwinds)
 *   * Wind effect indicator (tailwind assist / headwind delay / neutral crosswind)
 *   * Origin, Spherical Midpoint, and Destination coordinates
 * - Antipodal educational warning callout badge
 * - Minimizable mobile bottom drawer / desktop floating trigger
 */

/**
 * Converts decimal degrees latitude/longitude to formatted string with cardinal symbols.
 * Example: 48.8566, 2.3522 -> "48.86° N, 2.35° E"
 *
 * @param {number} lat
 * @param {number} lng
 * @returns {string}
 */
export function formatCoordinate(lat, lng) {
  if (typeof lat !== 'number' || typeof lng !== 'number' || isNaN(lat) || isNaN(lng)) {
    return '—';
  }
  const latDir = lat >= 0 ? 'N' : 'S';
  const lngDir = lng >= 0 ? 'E' : 'W';
  return `${Math.abs(lat).toFixed(2)}° ${latDir}, ${Math.abs(lng).toFixed(2)}° ${lngDir}`;
}

/**
 * Formats distance in km and miles with thousands separators.
 *
 * @param {number} km
 * @param {number} [miles]
 * @returns {{ km: string, miles: string }}
 */
export function formatDistance(km, miles) {
  if (typeof km !== 'number' || isNaN(km) || km < 0) {
    return { km: '— km', miles: '— mi' };
  }
  const mi = typeof miles === 'number' && !isNaN(miles) ? miles : km * 0.621371;
  const kmFormatted = Math.round(km).toLocaleString('en-US');
  const miFormatted = Math.round(mi).toLocaleString('en-US');
  return {
    km: `${kmFormatted} km`,
    miles: `${miFormatted} mi`
  };
}

/**
 * Formats duration in decimal hours to "Xh Ym" string.
 *
 * @param {number} hours
 * @returns {string}
 */
export function formatDuration(hours) {
  if (typeof hours !== 'number' || isNaN(hours) || hours <= 0) {
    return '0h 00m';
  }
  const totalMinutes = Math.round(hours * 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${h}h ${m < 10 ? '0' : ''}${m}m`;
}

/**
 * Generates an educational description of prevailing jet stream effects.
 *
 * @param {number} prevailingWindAdjustmentHours
 * @param {number} nominalFlightTimeHours
 * @returns {{ text: string, type: 'headwind' | 'tailwind' | 'neutral', penaltyPercent: number, penaltyMinutes: number }}
 */
export function formatWindPenalty(prevailingWindAdjustmentHours, nominalFlightTimeHours) {
  if (
    typeof prevailingWindAdjustmentHours !== 'number' ||
    isNaN(prevailingWindAdjustmentHours) ||
    (prevailingWindAdjustmentHours === 0 && (nominalFlightTimeHours === 0 || !nominalFlightTimeHours))
  ) {
    return {
      text: 'No active flight route',
      type: 'neutral',
      penaltyPercent: 0,
      penaltyMinutes: 0
    };
  }

  if (Math.abs(prevailingWindAdjustmentHours) < 0.05) {
    return {
      text: 'Favorable tailwind / Neutral flow (optimal eastward or crosswind corridor)',
      type: 'tailwind',
      penaltyPercent: 0,
      penaltyMinutes: 0
    };
  }

  const penaltyMinutes = Math.round(Math.abs(prevailingWindAdjustmentHours) * 60);
  const penaltyPercent =
    nominalFlightTimeHours > 0
      ? Math.round((Math.abs(prevailingWindAdjustmentHours) / nominalFlightTimeHours) * 1000) / 10
      : 0;

  if (prevailingWindAdjustmentHours < 0) {
    return {
      text: `-${penaltyPercent}% (-${penaltyMinutes}m) tailwind assist (westerly jet stream)`,
      type: 'tailwind',
      penaltyPercent: -penaltyPercent,
      penaltyMinutes: -penaltyMinutes
    };
  } else {
    return {
      text: `+${penaltyPercent}% (+${penaltyMinutes}m) headwind delay (westerly jet stream)`,
      type: 'headwind',
      penaltyPercent,
      penaltyMinutes
    };
  }
}

/**
 * Helper to normalize city data into a consistent object structure.
 */
function normalizeCityRecord(city) {
  if (!city) return null;
  if (typeof city === 'object' && !Array.isArray(city)) {
    return {
      id: city.id ?? null,
      kind: city.kind || 'city',
      name: city.name || '',
      country: city.country || '',
      lat: typeof city.lat === 'number' ? city.lat : 0,
      lng: typeof city.lng === 'number' ? city.lng : (typeof city.lon === 'number' ? city.lon : 0)
    };
  }
  if (Array.isArray(city) && city.length >= 4) {
    return {
      id: null,
      kind: city[4] || 'city',
      name: city[0] || '',
      country: city[1] || '',
      lat: typeof city[2] === 'number' ? city[2] : 0,
      lng: typeof city[3] === 'number' ? city[3] : 0
    };
  }
  return null;
}

export const ANTIPODAL_CALLOUT_TEXT = 'Antipodal pair (~180° separation) — Multiple geodesic paths exist; polar route via North Pole selected.';

/**
 * FlightHUD Component Class
 */
export class FlightHUD {
  /**
   * @param {HTMLElement} container - Container to mount the HUD into
   * @param {object} options - Configuration options
   */
  constructor(container, options = {}) {
    if (!container) {
      throw new Error('FlightHUD requires a valid container DOM element');
    }

    this.container = container;
    this.options = options;
    this.directory = options.directory || null;
    this.doc = options.document || container.ownerDocument || (typeof document !== 'undefined' ? document : null);

    if (!this.doc) {
      throw new Error('FlightHUD requires a DOM document');
    }

    // Callbacks
    this.onOriginSelect = options.onOriginSelect || (() => {});
    this.onDestSelect = options.onDestSelect || (() => {});
    this.onSwap = options.onSwap || (() => {});
    this.onRecenter = options.onRecenter || (() => {});
    this.onCollapseChange = options.onCollapseChange || (() => {});
    this.onProjectionChange = options.onProjectionChange || (() => {});
    // [eclipse-overlay] Optional layer switch: { visible, onToggle(visible) }.
    // The control is only built when this option is supplied.
    this.eclipseOptions = options.eclipseToggle || null;
    this.eclipseVisible = this.eclipseOptions ? this.eclipseOptions.visible !== false : false;
    this.eclipseToggle = null;
    this.eclipseBtn = null;
    this.eclipseCard = null;
    this.eclipseCardBody = null;

    // State
    this.originCity = normalizeCityRecord(options.initialOrigin);
    this.destCity = normalizeCityRecord(options.initialDest);
    this.projectionType = options.initialProjection || 'orthographic';
    this.geodesicResult = null;
    this.isCollapsed = false;

    // Elements
    this.projToggle = null;
    this.orthoBtn = null;
    this.robinsonBtn = null;

    // Combobox controllers
    this.originCombobox = null;
    this.destCombobox = null;

    // Build DOM structure
    this._buildHUD();

    // Set initial values if provided
    if (this.originCity) {
      this.originCombobox.setSelectedCity(this.originCity);
    }
    if (this.destCity) {
      this.destCombobox.setSelectedCity(this.destCity);
    }
  }

  /**
   * Builds the complete HUD DOM hierarchy.
   */
  _buildHUD() {
    const doc = this.doc;

    // Main HUD Card
    const hud = doc.createElement('aside');
    hud.className = 'geodesic-hud';
    hud.setAttribute('role', 'region');
    hud.setAttribute('aria-label', 'Flight Route & Telemetry Controls');

    // Mobile Sheet Handle
    const handleBar = doc.createElement('div');
    handleBar.className = 'hud-drawer-handle';
    handleBar.setAttribute('role', 'button');
    handleBar.setAttribute('aria-expanded', 'true');
    handleBar.setAttribute('tabindex', '0');
    handleBar.setAttribute('aria-label', 'Toggle flight telemetry drawer');
    handleBar.innerHTML = '<span class="hud-drawer-pill"></span>';
    handleBar.addEventListener('click', () => {
      this.toggleCollapse();
    });
    handleBar.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        if (typeof e.preventDefault === 'function') e.preventDefault();
        this.toggleCollapse();
      }
    });
    hud.appendChild(handleBar);

    // Floating trigger button when collapsed
    const floatingTrigger = doc.createElement('button');
    floatingTrigger.className = 'hud-floating-trigger hidden';
    floatingTrigger.type = 'button';
    floatingTrigger.setAttribute('title', 'Expand flight controls');
    floatingTrigger.setAttribute('aria-label', 'Expand flight controls');
    floatingTrigger.innerHTML = `
      <svg class="hud-icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="2" y1="12" x2="22" y2="12"></line>
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
      </svg>
      <span class="trigger-summary">Flight Controls</span>
    `;
    floatingTrigger.addEventListener('click', () => {
      this.expand();
    });
    this.container.appendChild(floatingTrigger);
    this.floatingTrigger = floatingTrigger;
    this.floatingSummaryEl = floatingTrigger.querySelector('.trigger-summary');

    // HUD Header
    const header = doc.createElement('header');
    header.className = 'hud-header';

    const headerLeft = doc.createElement('div');
    headerLeft.className = 'hud-branding';

    const titleBadge = doc.createElement('div');
    titleBadge.className = 'hud-status-pulse';
    titleBadge.setAttribute('aria-hidden', 'true');

    const titleGroup = doc.createElement('div');
    titleGroup.className = 'hud-title-group';

    const title = doc.createElement('h1');
    title.className = 'hud-title';
    title.textContent = 'GEODESIC FLIGHT';

    const subtitle = doc.createElement('p');
    subtitle.className = 'hud-subtitle';
    subtitle.textContent = 'Spherical Great-Circle Telemetry';

    titleGroup.appendChild(title);
    titleGroup.appendChild(subtitle);
    headerLeft.appendChild(titleBadge);
    headerLeft.appendChild(titleGroup);
    header.appendChild(headerLeft);

    // Header Actions Group
    const headerActions = doc.createElement('div');
    headerActions.className = 'hud-header-actions';

    // Projection Toggle (Globe / Robinson)
    const projToggle = doc.createElement('div');
    projToggle.className = 'hud-proj-toggle';
    projToggle.setAttribute('role', 'group');
    projToggle.setAttribute('aria-label', 'Map Projection Toggle');

    const orthoBtn = doc.createElement('button');
    orthoBtn.className = `hud-proj-btn hud-proj-ortho ${this.projectionType === 'orthographic' ? 'is-active' : ''}`;
    orthoBtn.type = 'button';
    orthoBtn.setAttribute('title', 'Switch to 3D Orthographic Globe projection');
    orthoBtn.setAttribute('aria-label', '3D Orthographic Globe projection');
    orthoBtn.setAttribute('aria-pressed', String(this.projectionType === 'orthographic'));
    orthoBtn.innerHTML = `
      <svg class="hud-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="2" y1="12" x2="22" y2="12"></line>
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
      </svg>
      <span class="btn-text">Globe</span>
    `;

    const robinsonBtn = doc.createElement('button');
    robinsonBtn.className = `hud-proj-btn hud-proj-robinson ${this.projectionType === 'robinson' ? 'is-active' : ''}`;
    robinsonBtn.type = 'button';
    robinsonBtn.setAttribute('title', 'Switch to 2D Robinson flat map projection centered on route');
    robinsonBtn.setAttribute('aria-label', '2D Robinson flat map projection');
    robinsonBtn.setAttribute('aria-pressed', String(this.projectionType === 'robinson'));
    robinsonBtn.innerHTML = `
      <svg class="hud-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"></polygon>
        <line x1="8" y1="2" x2="8" y2="18"></line>
        <line x1="16" y1="6" x2="16" y2="22"></line>
      </svg>
      <span class="btn-text">Robinson</span>
    `;

    orthoBtn.addEventListener('click', () => {
      this.setProjection('orthographic');
    });
    robinsonBtn.addEventListener('click', () => {
      this.setProjection('robinson');
    });

    projToggle.appendChild(orthoBtn);
    projToggle.appendChild(robinsonBtn);
    this.container.appendChild(projToggle);

    this.projToggle = projToggle;
    this.orthoBtn = orthoBtn;
    this.robinsonBtn = robinsonBtn;

    // [eclipse-overlay] Eclipse layer switch, mounted under the projection toggle.
    if (this.eclipseOptions) {
      const eclipseToggle = doc.createElement('div');
      eclipseToggle.className = 'hud-proj-toggle hud-eclipse-toggle';
      eclipseToggle.setAttribute('role', 'group');
      eclipseToggle.setAttribute('aria-label', 'Map overlays');

      const eclipseBtn = doc.createElement('button');
      eclipseBtn.className = `hud-proj-btn hud-eclipse-btn ${this.eclipseVisible ? 'is-active' : ''}`;
      eclipseBtn.type = 'button';
      eclipseBtn.setAttribute('title', 'Show or hide the 2 August 2027 total solar eclipse path');
      eclipseBtn.setAttribute('aria-label', 'Toggle the 2 August 2027 solar eclipse overlay');
      eclipseBtn.setAttribute('aria-pressed', String(this.eclipseVisible));
      eclipseBtn.innerHTML = `
        <svg class="hud-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="8"></circle>
          <path d="M12 4a8 8 0 0 1 0 16 6 6 0 0 0 0-16z" fill="currentColor" stroke="none" opacity="0.6"></path>
        </svg>
        <span class="btn-text">${this.eclipseOptions.label || 'Eclipse 2027'}</span>
      `;
      eclipseBtn.addEventListener('click', () => {
        const next = !this.eclipseVisible;
        this.setEclipseVisible(next);
        if (next) this.showEclipseCard();
      });

      eclipseToggle.appendChild(eclipseBtn);
      this.container.appendChild(eclipseToggle);
      this.eclipseToggle = eclipseToggle;
      this.eclipseBtn = eclipseBtn;

      // Short description card, shown when the overlay is switched on by click.
      const card = doc.createElement('div');
      card.className = 'hud-eclipse-card';
      card.setAttribute('role', 'note');
      card.setAttribute('aria-label', 'About the 2 August 2027 eclipse');
      card.style.display = 'none';
      const closeBtn = doc.createElement('button');
      closeBtn.className = 'hud-eclipse-card-close';
      closeBtn.type = 'button';
      closeBtn.setAttribute('aria-label', 'Dismiss eclipse description');
      closeBtn.textContent = '×';
      closeBtn.addEventListener('click', () => this.hideEclipseCard());
      const body = doc.createElement('div');
      body.className = 'hud-eclipse-card-body';
      card.appendChild(closeBtn);
      card.appendChild(body);
      this.container.appendChild(card);
      this.eclipseCard = card;
      this.eclipseCardBody = body;
    }

    const recenterBtn = doc.createElement('button');
    recenterBtn.className = 'hud-btn hud-btn-recenter';
    recenterBtn.type = 'button';
    recenterBtn.setAttribute('title', 'Recenter map to route midpoint');
    recenterBtn.setAttribute('aria-label', 'Recenter projection to route midpoint');
    recenterBtn.innerHTML = `
      <svg class="hud-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="9"></circle>
        <line x1="12" y1="2" x2="12" y2="6"></line>
        <line x1="12" y1="18" x2="12" y2="22"></line>
        <line x1="2" y1="12" x2="6" y2="12"></line>
        <line x1="18" y1="12" x2="22" y2="12"></line>
      </svg>
      <span class="btn-text">Recenter</span>
    `;
    recenterBtn.addEventListener('click', () => {
      this.onRecenter();
    });
    headerActions.appendChild(recenterBtn);

    const toggleBtn = doc.createElement('button');
    toggleBtn.className = 'hud-btn hud-btn-toggle';
    toggleBtn.type = 'button';
    toggleBtn.setAttribute('title', 'Minimize flight panel to view full map');
    toggleBtn.setAttribute('aria-label', 'Minimize flight panel to view full map');
    toggleBtn.innerHTML = `
      <svg class="hud-icon toggle-icon-collapse" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
      <svg class="hud-icon toggle-icon-expand" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display: none;">
        <polyline points="18 15 12 9 6 15"></polyline>
      </svg>
      <span class="btn-text">Hide</span>
    `;
    toggleBtn.addEventListener('click', () => {
      this.toggleCollapse();
    });
    headerActions.appendChild(toggleBtn);

    header.appendChild(headerActions);
    hud.appendChild(header);

    // Route Selection Section
    const routeSection = doc.createElement('section');
    routeSection.className = 'hud-route-section';
    routeSection.setAttribute('aria-label', 'City Selection');

    // Origin Combobox Container
    const originContainer = doc.createElement('div');
    originContainer.className = 'combobox-container combobox-origin combobox-group';

    const originLabel = doc.createElement('label');
    originLabel.className = 'combobox-label combobox-label-row';
    originLabel.textContent = 'ORIGIN';
    originLabel.htmlFor = 'hud-origin-input';

    const originBadge = doc.createElement('span');
    originBadge.className = 'combobox-dot indicator-amber';
    if (typeof originLabel.prepend === 'function') { originLabel.prepend(originBadge); } else { originLabel.appendChild(originBadge); }

    this.originCombobox = this._createCombobox({
      id: 'hud-origin-input',
      placeholder: 'Search origin (e.g. Singapore, London)...',
      ariaLabel: 'Search origin city',
      onSelect: (city) => {
        this.originCity = city;
        this.onOriginSelect(city);
      }
    });

    originContainer.appendChild(originLabel);
    originContainer.appendChild(this.originCombobox.inputWrapper);
    routeSection.appendChild(originContainer);

    // 1-Click Swap Button
    const swapContainer = doc.createElement('div');
    swapContainer.className = 'swap-btn-container';

    const swapBtn = doc.createElement('button');
    swapBtn.className = 'hud-btn-swap';
    swapBtn.type = 'button';
    swapBtn.setAttribute('title', 'Swap Origin and Destination');
    swapBtn.setAttribute('aria-label', 'Swap Origin and Destination cities');
    swapBtn.innerHTML = `
      <svg class="swap-icon" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M7 16V4M7 4L3 8M7 4L11 8"></path>
        <path d="M17 8V20M17 20L21 16M17 20L13 16"></path>
      </svg>
    `;
    swapBtn.addEventListener('click', () => {
      this.swapCities();
    });
    swapContainer.appendChild(swapBtn);
    routeSection.appendChild(swapContainer);

    // Destination Combobox Container
    const destContainer = doc.createElement('div');
    destContainer.className = 'combobox-container combobox-dest combobox-group';

    const destLabel = doc.createElement('label');
    destLabel.className = 'combobox-label combobox-label-row';
    destLabel.textContent = 'DESTINATION';
    destLabel.htmlFor = 'hud-dest-input';

    const destBadge = doc.createElement('span');
    destBadge.className = 'combobox-dot indicator-emerald';
    if (typeof destLabel.prepend === 'function') { destLabel.prepend(destBadge); } else { destLabel.appendChild(destBadge); }

    this.destCombobox = this._createCombobox({
      id: 'hud-dest-input',
      placeholder: 'Search destination (e.g. New York, London)...',
      ariaLabel: 'Search destination city',
      onSelect: (city) => {
        this.destCity = city;
        this.onDestSelect(city);
      }
    });

    destContainer.appendChild(destLabel);
    destContainer.appendChild(this.destCombobox.inputWrapper);
    routeSection.appendChild(destContainer);

    hud.appendChild(routeSection);

    // Antipodal Educational Callout Warning Badge
    const antipodalBadge = doc.createElement('div');
    antipodalBadge.className = 'hud-antipodal-badge';
    antipodalBadge.setAttribute('role', 'status');
    antipodalBadge.style.display = 'none';
    antipodalBadge.innerHTML = `
      <div class="antipodal-badge-inner">
        <svg class="antipodal-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="8" x2="12" y2="12"></line>
          <line x1="12" y1="16" x2="12.01" y2="16"></line>
        </svg>
        <span class="antipodal-message">${ANTIPODAL_CALLOUT_TEXT}</span>
      </div>
    `;
    hud.appendChild(antipodalBadge);
    this.antipodalBadge = antipodalBadge;

    // Flight Telemetry Card
    const telemetryCard = doc.createElement('section');
    telemetryCard.className = 'hud-telemetry-card';
    telemetryCard.setAttribute('aria-label', 'Flight Telemetry');

    telemetryCard.innerHTML = `
      <div class="telemetry-grid">
        <!-- Distance Metric -->
        <div class="telemetry-metric-block metric-distance">
          <div class="metric-label">GREAT CIRCLE DISTANCE</div>
          <div class="metric-value-primary" data-telemetry="distance-km">—</div>
          <div class="metric-value-secondary" data-telemetry="distance-mi">—</div>
        </div>

        <!-- Duration Metric -->
        <div class="telemetry-metric-block metric-duration">
          <div class="metric-label">FLIGHT TIME</div>
          <div class="metric-value-primary" data-telemetry="duration-effective">—</div>
          <div class="metric-value-secondary" data-telemetry="duration-nominal">—</div>
        </div>
      </div>

      <!-- Jet Stream Atmospheric Wind Penalty -->
      <div class="telemetry-wind-block" data-telemetry="wind-block">
        <div class="wind-label-row">
          <span class="wind-label">JET STREAM EFFECT</span>
          <span class="wind-badge" data-telemetry="wind-badge">Awaiting Route</span>
        </div>
        <div class="wind-description" data-telemetry="wind-desc">Select departure and arrival cities to calculate prevailing winds.</div>
      </div>

      <!-- Geographic Coordinates Row -->
      <div class="telemetry-coords-block">
        <div class="coord-item coord-origin">
          <span class="coord-tag origin-tag">ORIGIN</span>
          <span class="coord-val" data-telemetry="origin-coord">—</span>
        </div>
        <div class="coord-item coord-midpoint">
          <span class="coord-tag midpoint-tag">MIDPOINT</span>
          <span class="coord-val" data-telemetry="midpoint-coord">—</span>
        </div>
        <div class="coord-item coord-dest">
          <span class="coord-tag dest-tag">DESTINATION</span>
          <span class="coord-val" data-telemetry="dest-coord">—</span>
        </div>
      </div>
    `;
    hud.appendChild(telemetryCard);

    // Save metric elements for dynamic updates
    this.distanceKmEl = telemetryCard.querySelector('[data-telemetry="distance-km"]');
    this.distanceMilesEl = telemetryCard.querySelector('[data-telemetry="distance-mi"]');
    this.durationEffectiveEl = telemetryCard.querySelector('[data-telemetry="duration-effective"]');
    this.durationNominalEl = telemetryCard.querySelector('[data-telemetry="duration-nominal"]');
    this.windBadgeEl = telemetryCard.querySelector('[data-telemetry="wind-badge"]');
    this.windDescEl = telemetryCard.querySelector('[data-telemetry="wind-desc"]');
    this.originCoordEl = telemetryCard.querySelector('[data-telemetry="origin-coord"]');
    this.midpointCoordEl = telemetryCard.querySelector('[data-telemetry="midpoint-coord"]');
    this.destCoordEl = telemetryCard.querySelector('[data-telemetry="dest-coord"]');

    this.container.appendChild(hud);
    this.hudElement = hud;
    this.recenterBtn = recenterBtn;
    this.toggleBtn = toggleBtn;
    this.swapBtn = swapBtn;
    this.handleBar = handleBar;
    this.telemetryCard = telemetryCard;
  }

  /**
   * Helper to create an accessible combobox input with autocomplete dropdown.
   */
  _createCombobox(config) {
    const doc = this.doc;
    const directory = this.directory;

    const inputWrapper = doc.createElement('div');
    inputWrapper.className = 'combobox-wrapper';

    const input = doc.createElement('input');
    input.type = 'text';
    input.id = config.id;
    input.className = 'combobox-input';
    input.placeholder = config.placeholder;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-label', config.ariaLabel);
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');

    const clearBtn = doc.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'combobox-clear-btn';
    clearBtn.setAttribute('title', 'Clear selection');
    clearBtn.setAttribute('aria-label', 'Clear selection');
    clearBtn.innerHTML = `
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="18" y1="6" x2="6" y2="18"></line>
        <line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    `;
    clearBtn.style.display = 'none';

    const dropdown = doc.createElement('ul');
    dropdown.className = 'combobox-dropdown';
    dropdown.setAttribute('role', 'listbox');
    dropdown.setAttribute('aria-label', `${config.ariaLabel} results`);
    dropdown.style.display = 'none';

    inputWrapper.appendChild(input);
    inputWrapper.appendChild(clearBtn);
    inputWrapper.appendChild(dropdown);

    let activeIndex = -1;
    let results = [];
    let isOpen = false;
    let selectedCity = null;
    let debounceTimer = null;
    const minQueryLength = 2;
    const searchResultLimit = 10;
    const searchDebounceMs = 100;

    const openDropdown = () => {
      isOpen = true;
      dropdown.style.display = 'block';
      input.setAttribute('aria-expanded', 'true');
    };

    const closeDropdown = () => {
      isOpen = false;
      dropdown.style.display = 'none';
      input.setAttribute('aria-expanded', 'false');
      activeIndex = -1;
    };

    const updateActiveItem = (newIndex) => {
      const items = dropdown.querySelectorAll('.combobox-option');
      if (activeIndex >= 0 && items[activeIndex]) {
        items[activeIndex].classList.remove('active', 'is-active');
        items[activeIndex].setAttribute('aria-selected', 'false');
      }
      activeIndex = newIndex;
      if (activeIndex >= 0 && items[activeIndex]) {
        items[activeIndex].classList.add('active', 'is-active');
        items[activeIndex].setAttribute('aria-selected', 'true');
        if (typeof items[activeIndex].scrollIntoView === 'function') {
          items[activeIndex].scrollIntoView({ block: 'nearest' });
        }
      }
    };

    const renderResults = () => {
      dropdown.innerHTML = '';
      if (results.length === 0) {
        const hint = doc.createElement('li');
        hint.className = 'combobox-empty';
        hint.setAttribute('role', 'presentation');
        hint.textContent = input.value.trim().length === 1 ? 'Type one more letter…' : 'No matches';
        dropdown.appendChild(hint);
        activeIndex = -1;
        openDropdown();
        return;
      }

      results.forEach((city, index) => {
        const li = doc.createElement('li');
        li.className = 'combobox-option';
        li.setAttribute('role', 'option');
        li.setAttribute('id', `${config.id}-opt-${index}`);
        li.setAttribute('aria-selected', 'false');

        const nameSpan = doc.createElement('span');
        nameSpan.className = 'option-city-name';
        nameSpan.textContent = city.name;

        const countrySpan = doc.createElement('span');
        countrySpan.className = 'option-country-tag';
        countrySpan.textContent = city.country;

        const textRow = doc.createElement('div');
        textRow.className = 'option-text-row';
        textRow.appendChild(nameSpan);
        if (city.kind === 'place') {
          const chip = doc.createElement('span');
          chip.className = 'option-place-chip';
          chip.textContent = 'place';
          textRow.appendChild(chip);
        }
        textRow.appendChild(countrySpan);
        li.appendChild(textRow);

        li.addEventListener('mousedown', (e) => {
          if (e.preventDefault) e.preventDefault();
        });
        li.addEventListener('click', () => {
          selectCity(city);
        });

        dropdown.appendChild(li);
      });

      activeIndex = -1;
      openDropdown();
    };

    const executeSearch = (query) => {
      const q = (query || '').trim();
      if (q.length < minQueryLength) {
        results = [];
        closeDropdown();
        return;
      }

      if (directory && typeof directory.search === 'function') {
        results = directory.search(q, searchResultLimit);
      } else {
        results = [];
      }

      renderResults();
    };

    const scheduleSearch = (query) => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = null;
      results = [];
      closeDropdown();

      const q = (query || '').trim();
      if (q.length < minQueryLength) {
        dropdown.innerHTML = '';
        if (q.length === 1) renderResults();
        return;
      }

      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        executeSearch(input.value);
      }, searchDebounceMs);
    };

    const selectCity = (city) => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = null;
      selectedCity = city;
      input.value = `${city.name}, ${city.country}`;
      clearBtn.style.display = 'block';
      closeDropdown();
      config.onSelect(city);
      input.blur();
    };

    // Input handlers
    input.addEventListener('input', () => {
      const val = input.value;
      if (val.length > 0) {
        clearBtn.style.display = 'block';
      } else {
        clearBtn.style.display = 'none';
      }

      scheduleSearch(val);
    });

    const selectedLabel = () => selectedCity ? `${selectedCity.name}, ${selectedCity.country}` : '';
    input.addEventListener('focus', () => {
      this.hudElement.classList.add('is-searching');
      if (input.value !== selectedLabel()) scheduleSearch(input.value);
    });

    input.addEventListener('blur', () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = null;
      results = [];
      closeDropdown();
      input.value = selectedLabel();
      clearBtn.style.display = input.value ? 'block' : 'none';
      this.hudElement.classList.remove('is-searching');
    });

    // Keyboard Navigation
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') {
        if (!isOpen) {
          scheduleSearch(input.value);
        } else if (results.length > 0) {
          if (e.preventDefault) e.preventDefault();
          const nextIndex = activeIndex < results.length - 1 ? activeIndex + 1 : 0;
          updateActiveItem(nextIndex);
        }
      } else if (e.key === 'ArrowUp') {
        if (isOpen && results.length > 0) {
          if (e.preventDefault) e.preventDefault();
          const prevIndex = activeIndex > 0 ? activeIndex - 1 : results.length - 1;
          updateActiveItem(prevIndex);
        }
      } else if (e.key === 'Enter') {
        if (isOpen && activeIndex >= 0 && results[activeIndex]) {
          if (e.preventDefault) e.preventDefault();
          selectCity(results[activeIndex]);
        }
      } else if (e.key === 'Escape') {
        if (isOpen) {
          if (e.preventDefault) e.preventDefault();
          closeDropdown();
        }
      }
    });

    // Clear Button Handler
    clearBtn.addEventListener('mousedown', (e) => {
      if (e.preventDefault) e.preventDefault();
    });
    clearBtn.addEventListener('click', () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = null;
      input.value = '';
      clearBtn.style.display = 'none';
      results = [];
      closeDropdown();
      input.focus();
    });

    return {
      inputWrapper,
      input,
      dropdown,
      clearBtn,
      setValue: (text) => {
        input.value = text || '';
        clearBtn.style.display = input.value ? 'block' : 'none';
      },
      getValue: () => input.value,
      setSelectedCity: (city) => {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = null;
        results = [];
        closeDropdown();
        selectedCity = city;
        if (city) {
          input.value = `${city.name}, ${city.country}`;
          clearBtn.style.display = 'block';
        } else {
          input.value = '';
          clearBtn.style.display = 'none';
        }
      },
      getSelectedCity: () => selectedCity,
      search: (q) => scheduleSearch(q),
      getResults: () => results,
      isOpen: () => isOpen,
      getActiveIndex: () => activeIndex,
      close: () => closeDropdown(),
      destroy: () => {
        if (debounceTimer) clearTimeout(debounceTimer);
      }
    };
  }

  /**
   * 1-Click Swap: Reverses Origin and Destination cities with animation and state updates.
   */
  swapCities() {
    const tempOrigin = this.originCity;
    const tempDest = this.destCity;

    this.originCity = tempDest;
    this.destCity = tempOrigin;

    // Trigger visual rotation on button
    this.swapBtn.classList.add('swapping');
    setTimeout(() => {
      this.swapBtn.classList.remove('swapping');
    }, 350);

    // Update combobox inputs
    if (this.originCombobox) {
      this.originCombobox.setSelectedCity(this.originCity);
    }
    if (this.destCombobox) {
      this.destCombobox.setSelectedCity(this.destCity);
    }

    // Fire callbacks
    this.onSwap(this.originCity, this.destCity);
    this.onOriginSelect(this.originCity);
    this.onDestSelect(this.destCity);
  }

  /**
   * Updates all telemetry metrics based on geodesic engine result and selected endpoints.
   */
  updateTelemetry(geodesicResult, origin = null, dest = null) {
    this.geodesicResult = geodesicResult || null;
    if (origin) this.originCity = normalizeCityRecord(origin);
    if (dest) this.destCity = normalizeCityRecord(dest);

    if (!geodesicResult) {
      if (this.distanceKmEl) this.distanceKmEl.textContent = '— km';
      if (this.distanceMilesEl) this.distanceMilesEl.textContent = '— mi';
      if (this.durationEffectiveEl) this.durationEffectiveEl.textContent = '0h 00m';
      if (this.durationNominalEl) this.durationNominalEl.textContent = 'Cruise 870 km/h';
      if (this.windBadgeEl) {
        this.windBadgeEl.className = 'wind-badge wind-badge-neutral';
        this.windBadgeEl.textContent = 'Neutral';
      }
      if (this.windDescEl) this.windDescEl.textContent = 'No active flight route';
      if (this.originCoordEl) this.originCoordEl.textContent = '—';
      if (this.midpointCoordEl) this.midpointCoordEl.textContent = '—';
      if (this.destCoordEl) this.destCoordEl.textContent = '—';
      if (this.antipodalBadge) this.antipodalBadge.style.display = 'none';
      if (this.floatingSummaryEl) this.floatingSummaryEl.textContent = 'Flight Controls';
      return;
    }

    const {
      distanceKm,
      distanceMiles,
      nominalFlightTimeHours,
      effectiveFlightTimeHours,
      prevailingWindAdjustmentHours,
      sphericalMidpoint,
      isAntipodal
    } = geodesicResult;

    // 1. Distance
    const dist = formatDistance(distanceKm, distanceMiles);
    if (this.floatingSummaryEl) {
      if (this.originCity && this.destCity && dist) {
        this.floatingSummaryEl.textContent = `${this.originCity.name} → ${this.destCity.name} · ${dist.km}`;
      } else {
        this.floatingSummaryEl.textContent = 'Flight Controls';
      }
    }
    if (this.distanceKmEl) this.distanceKmEl.textContent = dist.km;
    if (this.distanceMilesEl) this.distanceMilesEl.textContent = dist.miles;

    // 2. Flight Duration
    const effHours = typeof effectiveFlightTimeHours === 'number'
      ? effectiveFlightTimeHours
      : nominalFlightTimeHours + (prevailingWindAdjustmentHours || 0);
    const nomHours = typeof nominalFlightTimeHours === 'number'
      ? nominalFlightTimeHours
      : (distanceKm / 870);

    if (this.durationEffectiveEl) {
      this.durationEffectiveEl.textContent = formatDuration(effHours);
    }
    if (this.durationNominalEl) {
      if (Math.abs(prevailingWindAdjustmentHours) > 0.05) {
        this.durationNominalEl.textContent = `Nominal ${formatDuration(nomHours)} · 870 km/h`;
      } else {
        this.durationNominalEl.textContent = 'Cruise 870 km/h';
      }
    }

    // 3. Jet Stream Prevailing Wind Breakdown
    const windInfo = formatWindPenalty(prevailingWindAdjustmentHours, nomHours);
    if (this.windBadgeEl) {
      this.windBadgeEl.className = `wind-badge wind-badge-${windInfo.type}`;
      if (windInfo.type === 'headwind') {
        this.windBadgeEl.textContent = `+${windInfo.penaltyMinutes}m Headwind`;
      } else if (windInfo.type === 'tailwind') {
        if (windInfo.penaltyMinutes < 0) {
          this.windBadgeEl.textContent = `${windInfo.penaltyMinutes}m Tailwind`;
        } else {
          this.windBadgeEl.textContent = 'Optimal Tailwind';
        }
      } else {
        this.windBadgeEl.textContent = 'Neutral';
      }
    }
    if (this.windDescEl) {
      this.windDescEl.textContent = windInfo.text;
    }

    // 4. Coordinates
    if (this.originCoordEl) {
      this.originCoordEl.textContent = this.originCity
        ? formatCoordinate(this.originCity.lat, this.originCity.lng)
        : '—';
    }
    if (this.destCoordEl) {
      this.destCoordEl.textContent = this.destCity
        ? formatCoordinate(this.destCity.lat, this.destCity.lng)
        : '—';
    }
    if (this.midpointCoordEl) {
      if (sphericalMidpoint) {
        const midLat = typeof sphericalMidpoint.lat === 'number'
          ? sphericalMidpoint.lat
          : (Array.isArray(sphericalMidpoint) ? sphericalMidpoint[1] : 0);
        const midLng = typeof sphericalMidpoint.lng === 'number'
          ? sphericalMidpoint.lng
          : (Array.isArray(sphericalMidpoint) ? sphericalMidpoint[0] : 0);
        this.midpointCoordEl.textContent = formatCoordinate(midLat, midLng);
      } else {
        this.midpointCoordEl.textContent = '—';
      }
    }

    // 5. Antipodal Educational Callout Badge
    if (this.antipodalBadge) {
      this.antipodalBadge.style.display = isAntipodal ? 'block' : 'none';
    }
  }

  /**
   * Switches the active projection between 'orthographic' and 'robinson'.
   */
  setProjection(type) {
    if (type !== 'orthographic' && type !== 'robinson') return;
    this.projectionType = type;
    if (this.orthoBtn && this.robinsonBtn) {
      if (type === 'orthographic') {
        this.orthoBtn.classList.add('is-active');
        this.orthoBtn.setAttribute('aria-pressed', 'true');
        this.robinsonBtn.classList.remove('is-active');
        this.robinsonBtn.setAttribute('aria-pressed', 'false');
      } else {
        this.robinsonBtn.classList.add('is-active');
        this.robinsonBtn.setAttribute('aria-pressed', 'true');
        this.orthoBtn.classList.remove('is-active');
        this.orthoBtn.setAttribute('aria-pressed', 'false');
      }
    }
    if (typeof this.onProjectionChange === 'function') {
      this.onProjectionChange(this.projectionType);
    }
  }

  /**
   * Returns current active projection name ('orthographic' | 'robinson').
   */
  getProjection() {
    return this.projectionType;
  }

  /**
   * [eclipse-overlay] Shows or hides the eclipse overlay and notifies the owner.
   */
  setEclipseVisible(visible) {
    if (!this.eclipseOptions) return;
    const next = !!visible;
    if (next === this.eclipseVisible) return;
    this.eclipseVisible = next;
    if (this.eclipseBtn) {
      this.eclipseBtn.classList.toggle('is-active', next);
      this.eclipseBtn.setAttribute('aria-pressed', String(next));
    }
    if (!next) this.hideEclipseCard();
    if (typeof this.eclipseOptions.onToggle === 'function') {
      this.eclipseOptions.onToggle(next);
    }
  }

  /** [eclipse-overlay] */
  getEclipseVisible() {
    return this.eclipseVisible;
  }

  /**
   * [eclipse-overlay] Fills and shows the description card. Content comes from
   * eclipseToggle.getDescription(), which returns { title, lines: string[] }.
   */
  showEclipseCard() {
    if (!this.eclipseCard || !this.eclipseOptions) return;
    const getter = this.eclipseOptions.getDescription;
    const desc = (typeof getter === 'function' ? getter() : null) || { title: 'Total solar eclipse', lines: [] };
    const doc = this.doc;
    const body = this.eclipseCardBody;
    while (body.firstChild) body.removeChild(body.firstChild);
    const title = doc.createElement('div');
    title.className = 'hud-eclipse-card-title';
    title.textContent = desc.title || '';
    body.appendChild(title);
    for (const line of desc.lines || []) {
      const p = doc.createElement('p');
      p.className = 'hud-eclipse-card-line';
      p.textContent = line;
      body.appendChild(p);
    }
    this.eclipseCard.style.display = '';
  }

  /** [eclipse-overlay] */
  hideEclipseCard() {
    if (this.eclipseCard) this.eclipseCard.style.display = 'none';
  }

  /** [eclipse-overlay] */
  isEclipseCardVisible() {
    return !!this.eclipseCard && this.eclipseCard.style.display !== 'none';
  }

  /**
   * Set Origin city programmatically.
   */
  setOrigin(city, triggerCallback = false) {
    this.originCity = normalizeCityRecord(city);
    if (this.originCombobox) {
      this.originCombobox.setSelectedCity(this.originCity);
    }
    if (triggerCallback) {
      this.onOriginSelect(this.originCity);
    }
  }

  /**
   * Set Destination city programmatically.
   */
  setDest(city, triggerCallback = false) {
    this.destCity = normalizeCityRecord(city);
    if (this.destCombobox) {
      this.destCombobox.setSelectedCity(this.destCity);
    }
    if (triggerCallback) {
      this.onDestSelect(this.destCity);
    }
  }

  getOrigin() {
    return this.originCity;
  }

  getDest() {
    return this.destCity;
  }

  /**
   * Collapses/minimizes the HUD panel, revealing the full globe.
   */
  collapse() {
    this.isCollapsed = true;
    this.hudElement.classList.add('is-minimized');
    if (this.handleBar) this.handleBar.setAttribute('aria-expanded', 'false');
    if (this.floatingTrigger) this.floatingTrigger.classList.remove('hidden');
    if (this.toggleBtn) {
      const textEl = this.toggleBtn.querySelector('.btn-text');
      if (textEl) textEl.textContent = 'Show';
      const cIcon = this.toggleBtn.querySelector('.toggle-icon-collapse');
      if (cIcon) cIcon.style.display = 'none';
      const eIcon = this.toggleBtn.querySelector('.toggle-icon-expand');
      if (eIcon) eIcon.style.display = '';
    }
    if (typeof this.onCollapseChange === 'function') {
      this.onCollapseChange(true);
    }
  }

  /**
   * Expands the HUD panel to display complete flight controls and telemetry.
   */
  expand() {
    this.isCollapsed = false;
    this.hudElement.classList.remove('is-minimized');
    if (this.handleBar) this.handleBar.setAttribute('aria-expanded', 'true');
    if (this.floatingTrigger) this.floatingTrigger.classList.add('hidden');
    if (this.toggleBtn) {
      const textEl = this.toggleBtn.querySelector('.btn-text');
      if (textEl) textEl.textContent = 'Hide';
      const cIcon = this.toggleBtn.querySelector('.toggle-icon-collapse');
      if (cIcon) cIcon.style.display = '';
      const eIcon = this.toggleBtn.querySelector('.toggle-icon-expand');
      if (eIcon) eIcon.style.display = 'none';
    }
    if (typeof this.onCollapseChange === 'function') {
      this.onCollapseChange(false);
    }
  }

  /**
   * Toggles between expanded and minimized panel states.
   */
  toggleCollapse() {
    if (this.isCollapsed) {
      this.expand();
    } else {
      this.collapse();
    }
  }

  /**
   * Returns current collapse status.
   */
  getIsCollapsed() {
    return this.isCollapsed;
  }

  /**
   * Returns a map of key DOM elements for automated test assertions.
   */
  getElements() {
    return {
      hud: this.hudElement,
      originInput: this.originCombobox?.input,
      originDropdown: this.originCombobox?.dropdown,
      originClearBtn: this.originCombobox?.clearBtn,
      destInput: this.destCombobox?.input,
      destDropdown: this.destCombobox?.dropdown,
      destClearBtn: this.destCombobox?.clearBtn,
      swapBtn: this.swapBtn,
      recenterBtn: this.recenterBtn,
      toggleBtn: this.toggleBtn,
      projToggle: this.projToggle,
      projectionToggle: this.projToggle,
      orthoBtn: this.orthoBtn,
      robinsonBtn: this.robinsonBtn,
      eclipseToggle: this.eclipseToggle, // [eclipse-overlay]
      eclipseBtn: this.eclipseBtn, // [eclipse-overlay]
      eclipseCard: this.eclipseCard, // [eclipse-overlay]
      handleBar: this.handleBar,
      floatingTrigger: this.floatingTrigger,
      floatingSummary: this.floatingSummaryEl,
      antipodalBadge: this.antipodalBadge,
      telemetryCard: this.telemetryCard,
      distanceKm: this.distanceKmEl,
      distanceMiles: this.distanceMilesEl,
      durationEffective: this.durationEffectiveEl,
      durationNominal: this.durationNominalEl,
      windBadge: this.windBadgeEl,
      windDesc: this.windDescEl,
      originCoord: this.originCoordEl,
      midpointCoord: this.midpointCoordEl,
      destCoord: this.destCoordEl
    };
  }

  /**
   * Teardown and cleanup routine.
   */
  destroy() {
    if (this.originCombobox) this.originCombobox.destroy();
    if (this.destCombobox) this.destCombobox.destroy();
    if (this.hudElement && this.hudElement.parentElement) {
      this.hudElement.parentElement.removeChild(this.hudElement);
    }
    if (this.projToggle && this.projToggle.parentElement) {
      this.projToggle.parentElement.removeChild(this.projToggle);
    }
    if (this.eclipseToggle && this.eclipseToggle.parentElement) { // [eclipse-overlay]
      this.eclipseToggle.parentElement.removeChild(this.eclipseToggle);
    }
    if (this.eclipseCard && this.eclipseCard.parentElement) { // [eclipse-overlay]
      this.eclipseCard.parentElement.removeChild(this.eclipseCard);
    }
    if (this.floatingTrigger && this.floatingTrigger.parentElement) {
      this.floatingTrigger.parentElement.removeChild(this.floatingTrigger);
    }
  }
}

/**
 * Factory function to create and mount FlightHUD.
 *
 * @param {HTMLElement} container
 * @param {object} options
 * @returns {FlightHUD}
 */
export function createFlightHUD(container, options = {}) {
  return new FlightHUD(container, options);
}
