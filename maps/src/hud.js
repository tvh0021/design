/**
 * Geodesic Flight Visualizer - HUD & Navigation UI Component
 *
 * Provides:
 * - Searchable Combobox Autocomplete for Origin & Destination cities
 * - 1-Click Swap Route button with rotation transition
 * - Recenter Globe camera button
 * - Flight Telemetry HUD Card (Distance, Duration, Wind Penalty, Coordinates)
 * - Educational Antipodal Warning Badge for ~180° spherical path ambiguity
 * - Full Keyboard Navigation (ArrowUp, ArrowDown, Enter, Escape)
 * - Responsive floating HUD / mobile sheet layout
 */

/**
 * Formats a latitude/longitude pair into a human-readable string.
 * Example: (48.8566, 2.3522) -> "48.86° N, 2.35° E"
 *
 * @param {number} lat - Latitude in degrees [-90, 90]
 * @param {number} lng - Longitude in degrees [-180, 180]
 * @returns {string}
 */
export function formatCoordinate(lat, lng) {
  if (typeof lat !== 'number' || typeof lng !== 'number' || isNaN(lat) || isNaN(lng)) {
    return '—';
  }
  const latDir = lat >= 0 ? 'N' : 'S';
  const lngDir = lng >= 0 ? 'E' : 'W';
  const latStr = `${Math.abs(lat).toFixed(2)}° ${latDir}`;
  const lngStr = `${Math.abs(lng).toFixed(2)}° ${lngDir}`;
  return `${latStr}, ${lngStr}`;
}

/**
 * Formats distances in kilometers and statute miles with comma thousands separators.
 *
 * @param {number} distanceKm
 * @param {number} distanceMiles
 * @returns {{ km: string, miles: string }}
 */
export function formatDistance(distanceKm, distanceMiles) {
  if (typeof distanceKm !== 'number' || isNaN(distanceKm) || distanceKm <= 0) {
    return { km: '0 km', miles: '0 mi' };
  }
  const kmFormatted = `${Math.round(distanceKm).toLocaleString('en-US')} km`;
  const milesVal = typeof distanceMiles === 'number' && !isNaN(distanceMiles)
    ? distanceMiles
    : distanceKm * 0.621371;
  const milesFormatted = `${Math.round(milesVal).toLocaleString('en-US')} mi`;
  return { km: kmFormatted, miles: milesFormatted };
}

/**
 * Formats decimal hours into a concise "Xh Ym" duration string.
 *
 * @param {number} hours
 * @returns {string}
 */
export function formatDuration(hours) {
  if (typeof hours !== 'number' || isNaN(hours) || hours <= 0) {
    return '0h 00m';
  }
  let totalMinutes = Math.round(hours * 60);
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
  if (typeof prevailingWindAdjustmentHours !== 'number' || isNaN(prevailingWindAdjustmentHours) || prevailingWindAdjustmentHours <= 0) {
    if (nominalFlightTimeHours > 0) {
      return {
        text: 'Favorable tailwind / Neutral flow (optimal eastward corridor)',
        type: 'tailwind',
        penaltyPercent: 0,
        penaltyMinutes: 0
      };
    }
    return {
      text: 'No active flight route',
      type: 'neutral',
      penaltyPercent: 0,
      penaltyMinutes: 0
    };
  }

  const penaltyMinutes = Math.round(prevailingWindAdjustmentHours * 60);
  const penaltyPercent = nominalFlightTimeHours > 0
    ? Math.round((prevailingWindAdjustmentHours / nominalFlightTimeHours) * 1000) / 10
    : 0;

  return {
    text: `+${penaltyPercent}% (+${penaltyMinutes}m) headwind delay (westerly jet stream)`,
    type: 'headwind',
    penaltyPercent,
    penaltyMinutes
  };
}

/**
 * Helper to normalize city data into a consistent object structure.
 */
function normalizeCityRecord(city) {
  if (!city) return null;
  if (typeof city === 'object' && !Array.isArray(city)) {
    return {
      id: city.id ?? null,
      name: city.name || '',
      country: city.country || '',
      lat: typeof city.lat === 'number' ? city.lat : 0,
      lng: typeof city.lng === 'number' ? city.lng : (typeof city.lon === 'number' ? city.lon : 0)
    };
  }
  if (Array.isArray(city) && city.length >= 4) {
    return {
      id: null,
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

    // State
    this.originCity = normalizeCityRecord(options.initialOrigin);
    this.destCity = normalizeCityRecord(options.initialDest);
    this.geodesicResult = null;
    this.isCollapsed = false;

    // Combobox controllers
    this.originCombobox = null;
    this.destCombobox = null;

    // Build DOM structure
    this._buildHUD();

    // Set initial values if provided
    if (this.originCity) {
      this.originCombobox.setValue(`${this.originCity.name}, ${this.originCity.country}`);
    }
    if (this.destCity) {
      this.destCombobox.setValue(`${this.destCity.name}, ${this.destCity.country}`);
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
    handleBar.setAttribute('tabindex', '0');
    handleBar.setAttribute('aria-label', 'Toggle flight controls panel');
    handleBar.setAttribute('aria-expanded', 'true');
    const handleIndicator = doc.createElement('span');
    handleIndicator.className = 'hud-drawer-pill';
    handleBar.appendChild(handleIndicator);
    hud.appendChild(handleBar);

    handleBar.addEventListener('click', () => {
      this.toggleCollapse();
    });
    handleBar.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        if (typeof e.preventDefault === 'function') e.preventDefault();
        this.toggleCollapse();
      }
    });

    let touchStartY = 0;
    let touchStartTime = 0;
    handleBar.addEventListener('touchstart', (e) => {
      if (e.touches && e.touches[0]) {
        touchStartY = e.touches[0].clientY;
        touchStartTime = Date.now();
      }
    }, { passive: true });

    handleBar.addEventListener('touchend', (e) => {
      if (e.changedTouches && e.changedTouches[0]) {
        const deltaY = e.changedTouches[0].clientY - touchStartY;
        const deltaTime = Date.now() - touchStartTime;
        if (deltaY > 30 && deltaTime < 600) {
          this.collapse();
        } else if (deltaY < -30 && deltaTime < 600) {
          this.expand();
        }
      }
    }, { passive: true });

    // Header Bar
    const header = doc.createElement('header');
    header.className = 'hud-header';

    const headerLeft = doc.createElement('div');
    headerLeft.className = 'hud-branding';

    const titleBadge = doc.createElement('div');
    titleBadge.className = 'hud-status-pulse';
    titleBadge.setAttribute('aria-hidden', 'true');

    const titleGroup = doc.createElement('div');
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

    const recenterBtn = doc.createElement('button');
    recenterBtn.className = 'hud-btn hud-btn-recenter';
    recenterBtn.type = 'button';
    recenterBtn.setAttribute('title', 'Recenter globe to route midpoint');
    recenterBtn.setAttribute('aria-label', 'Recenter globe projection to route midpoint');
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
    toggleBtn.setAttribute('title', 'Minimize flight panel to view full globe');
    toggleBtn.setAttribute('aria-label', 'Minimize flight panel to view full globe');
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
    originContainer.className = 'combobox-container combobox-origin';

    // Destination Combobox Container
    const destContainer = doc.createElement('div');
    destContainer.className = 'combobox-container combobox-dest';

    // 1-Click Swap Button
    const swapContainer = doc.createElement('div');
    swapContainer.className = 'swap-btn-container';

    const swapBtn = doc.createElement('button');
    swapBtn.className = 'hud-btn-swap';
    swapBtn.type = 'button';
    swapBtn.setAttribute('title', 'Swap Origin and Destination');
    swapBtn.setAttribute('aria-label', 'Swap origin and destination cities');
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

    // Append route inputs
    routeSection.appendChild(originContainer);
    routeSection.appendChild(swapContainer);
    routeSection.appendChild(destContainer);
    hud.appendChild(routeSection);

    // Antipodal Educational Warning Badge
    const antipodalBadge = doc.createElement('div');
    antipodalBadge.className = 'hud-antipodal-badge';
    antipodalBadge.setAttribute('role', 'alert');
    antipodalBadge.setAttribute('aria-live', 'polite');
    antipodalBadge.style.display = 'none';
    antipodalBadge.innerHTML = `
      <div class="antipodal-badge-inner">
        <span class="antipodal-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
            <line x1="12" y1="9" x2="12" y2="13"></line>
            <line x1="12" y1="17" x2="12.01" y2="17"></line>
          </svg>
        </span>
        <span class="antipodal-message">${ANTIPODAL_CALLOUT_TEXT}</span>
      </div>
    `;
    hud.appendChild(antipodalBadge);

    // Telemetry Card Section
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
          <div class="metric-label">FLIGHT DURATION <span class="metric-sublabel">@ 900 km/h</span></div>
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

    this.container.appendChild(hud);

    // Floating Trigger Pill for when HUD is minimized
    const floatingTrigger = doc.createElement('button');
    floatingTrigger.className = 'hud-floating-trigger hidden';
    floatingTrigger.type = 'button';
    floatingTrigger.setAttribute('title', 'Open flight controls');
    floatingTrigger.setAttribute('aria-label', 'Open flight route controls');
    floatingTrigger.innerHTML = `
      <span class="trigger-icon" aria-hidden="true">✈️</span>
      <span class="trigger-summary" data-telemetry="floating-summary">Flight Controls</span>
      <svg class="trigger-chevron" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="18 15 12 9 6 15"></polyline>
      </svg>
    `;
    floatingTrigger.addEventListener('click', () => {
      this.expand();
    });
    this.container.appendChild(floatingTrigger);

    // Store references
    this.hudElement = hud;
    this.handleBar = handleBar;
    this.recenterBtn = recenterBtn;
    this.toggleBtn = toggleBtn;
    this.floatingTrigger = floatingTrigger;
    this.floatingSummaryEl = floatingTrigger.querySelector('[data-telemetry="floating-summary"]');
    this.swapBtn = swapBtn;
    this.antipodalBadge = antipodalBadge;
    this.telemetryCard = telemetryCard;

    // Cache Telemetry query targets
    this.distanceKmEl = telemetryCard.querySelector('[data-telemetry="distance-km"]');
    this.distanceMilesEl = telemetryCard.querySelector('[data-telemetry="distance-mi"]');
    this.durationEffectiveEl = telemetryCard.querySelector('[data-telemetry="duration-effective"]');
    this.durationNominalEl = telemetryCard.querySelector('[data-telemetry="duration-nominal"]');
    this.windBadgeEl = telemetryCard.querySelector('[data-telemetry="wind-badge"]');
    this.windDescEl = telemetryCard.querySelector('[data-telemetry="wind-desc"]');
    this.originCoordEl = telemetryCard.querySelector('[data-telemetry="origin-coord"]');
    this.midpointCoordEl = telemetryCard.querySelector('[data-telemetry="midpoint-coord"]');
    this.destCoordEl = telemetryCard.querySelector('[data-telemetry="dest-coord"]');

    // Mount Comboboxes
    this.originCombobox = this._createCombobox({
      container: originContainer,
      label: 'ORIGIN',
      indicatorClass: 'indicator-amber',
      placeholder: 'Search origin city (e.g. Paris)...',
      inputId: 'hud-origin-input',
      onSelect: (city) => {
        this.originCity = city;
        this.onOriginSelect(city);
      }
    });

    this.destCombobox = this._createCombobox({
      container: destContainer,
      label: 'DESTINATION',
      indicatorClass: 'indicator-emerald',
      placeholder: 'Search destination (e.g. Tokyo)...',
      inputId: 'hud-dest-input',
      onSelect: (city) => {
        this.destCity = city;
        this.onDestSelect(city);
      }
    });
  }

  /**
   * Instantiates a fully accessible combobox with live search and keyboard navigation.
   */
  _createCombobox({ container, label, indicatorClass, placeholder, inputId, onSelect }) {
    const doc = this.doc;
    const directory = this.directory;

    const group = doc.createElement('div');
    group.className = 'combobox-group';

    // Label with accent dot
    const labelRow = doc.createElement('div');
    labelRow.className = 'combobox-label-row';

    const dot = doc.createElement('span');
    dot.className = `combobox-dot ${indicatorClass}`;
    dot.setAttribute('aria-hidden', 'true');

    const labelEl = doc.createElement('label');
    labelEl.className = 'combobox-label';
    labelEl.htmlFor = inputId;
    labelEl.textContent = label;

    labelRow.appendChild(dot);
    labelRow.appendChild(labelEl);
    group.appendChild(labelRow);

    // Input Wrapper
    const wrapper = doc.createElement('div');
    wrapper.className = 'combobox-wrapper';

    const input = doc.createElement('input');
    input.id = inputId;
    input.type = 'text';
    input.className = 'combobox-input';
    input.placeholder = placeholder;
    input.autocomplete = 'off';
    input.spellcheck = false;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');

    // Clear Button
    const clearBtn = doc.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'combobox-clear-btn';
    clearBtn.setAttribute('aria-label', `Clear ${label.toLowerCase()} city`);
    clearBtn.setAttribute('tabindex', '-1');
    clearBtn.innerHTML = '&times;';
    clearBtn.style.display = 'none';

    // Dropdown Listbox
    const dropdown = doc.createElement('ul');
    dropdown.className = 'combobox-dropdown';
    dropdown.setAttribute('role', 'listbox');
    dropdown.setAttribute('aria-label', `${label} suggestions`);
    dropdown.style.display = 'none';

    wrapper.appendChild(input);
    wrapper.appendChild(clearBtn);
    wrapper.appendChild(dropdown);
    group.appendChild(wrapper);
    container.appendChild(group);

    // State for this combobox instance
    let results = [];
    let activeIndex = -1;
    let isOpen = false;
    let debounceTimer = null;
    let selectedCity = null;

    const openDropdown = () => {
      if (results.length > 0) {
        dropdown.style.display = 'block';
        isOpen = true;
        input.setAttribute('aria-expanded', 'true');
      }
    };

    const closeDropdown = () => {
      dropdown.style.display = 'none';
      isOpen = false;
      activeIndex = -1;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
    };

    const updateActiveItem = (newIndex) => {
      const items = dropdown.children;
      for (let i = 0; i < items.length; i++) {
        if (i === newIndex) {
          items[i].classList.add('active');
          items[i].setAttribute('aria-selected', 'true');
          input.setAttribute('aria-activedescendant', items[i].id);
          // Auto-scroll inside dropdown if needed
          if (typeof items[i].scrollIntoView === 'function') {
            items[i].scrollIntoView({ block: 'nearest' });
          }
        } else {
          items[i].classList.remove('active');
          items[i].setAttribute('aria-selected', 'false');
        }
      }
      activeIndex = newIndex;
    };

    const renderResults = () => {
      // Clear dropdown
      while (dropdown.firstChild) {
        dropdown.removeChild(dropdown.firstChild);
      }

      if (results.length === 0) {
        const emptyItem = doc.createElement('li');
        emptyItem.className = 'combobox-empty';
        emptyItem.textContent = 'No matching cities found';
        dropdown.appendChild(emptyItem);
        openDropdown();
        return;
      }

      results.forEach((city, index) => {
        const item = doc.createElement('li');
        item.id = `${inputId}-option-${index}`;
        item.className = 'combobox-option';
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', 'false');

        const mainText = doc.createElement('span');
        mainText.className = 'option-city-name';
        mainText.textContent = city.name;

        const countryTag = doc.createElement('span');
        countryTag.className = 'option-country-tag';
        countryTag.textContent = city.country;

        const coordHint = doc.createElement('span');
        coordHint.className = 'option-coord-hint';
        coordHint.textContent = `${Math.abs(city.lat).toFixed(1)}°${city.lat >= 0 ? 'N' : 'S'}, ${Math.abs(city.lng).toFixed(1)}°${city.lng >= 0 ? 'E' : 'W'}`;

        const textRow = doc.createElement('div');
        textRow.className = 'option-text-row';
        textRow.appendChild(mainText);
        textRow.appendChild(countryTag);

        item.appendChild(textRow);
        item.appendChild(coordHint);

        item.addEventListener('mousedown', (e) => {
          // Prevent input blur before click registers
          if (e.preventDefault) e.preventDefault();
        });

        item.addEventListener('click', () => {
          selectCity(city);
        });

        item.addEventListener('mouseenter', () => {
          updateActiveItem(index);
        });

        dropdown.appendChild(item);
      });

      activeIndex = -1;
      openDropdown();
    };

    const executeSearch = (query) => {
      const q = (query || '').trim();
      if (!q) {
        results = [];
        closeDropdown();
        return;
      }

      if (directory && typeof directory.search === 'function') {
        results = directory.search(q, 8);
      } else {
        results = [];
      }

      renderResults();
    };

    const selectCity = (city) => {
      selectedCity = city;
      input.value = `${city.name}, ${city.country}`;
      clearBtn.style.display = 'block';
      closeDropdown();
      onSelect(city);
    };

    // Input handlers
    input.addEventListener('input', (e) => {
      const val = input.value;
      if (val.length > 0) {
        clearBtn.style.display = 'block';
      } else {
        clearBtn.style.display = 'none';
        selectedCity = null;
        onSelect(null);
      }

      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        executeSearch(input.value);
      }, 50);
    });

    input.addEventListener('focus', () => {
      if (input.value.trim().length > 0 && results.length > 0) {
        openDropdown();
      } else if (input.value.trim().length > 0) {
        executeSearch(input.value);
      }
    });

    input.addEventListener('blur', () => {
      // Small timeout to allow mousedown on options to execute
      setTimeout(() => {
        closeDropdown();
      }, 150);
    });

    // Keyboard Navigation
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') {
        if (!isOpen) {
          executeSearch(input.value);
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
    clearBtn.addEventListener('click', () => {
      input.value = '';
      clearBtn.style.display = 'none';
      selectedCity = null;
      results = [];
      closeDropdown();
      input.focus();
      onSelect(null);
    });

    return {
      input,
      dropdown,
      clearBtn,
      setValue: (text) => {
        input.value = text || '';
        clearBtn.style.display = input.value ? 'block' : 'none';
      },
      getValue: () => input.value,
      setSelectedCity: (city) => {
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
      search: (q) => executeSearch(q),
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
   *
   * @param {object|null} geodesicResult - Computed geodesic result from GeodesicEngine
   * @param {object|null} [origin] - Optional override for origin city
   * @param {object|null} [dest] - Optional override for destination city
   */
  updateTelemetry(geodesicResult, origin = null, dest = null) {
    this.geodesicResult = geodesicResult;
    if (origin !== null) this.originCity = normalizeCityRecord(origin);
    if (dest !== null) this.destCity = normalizeCityRecord(dest);

    if (!geodesicResult || typeof geodesicResult.distanceKm !== 'number') {
      // Reset to idle state
      if (this.distanceKmEl) this.distanceKmEl.textContent = '—';
      if (this.distanceMilesEl) this.distanceMilesEl.textContent = '—';
      if (this.durationEffectiveEl) this.durationEffectiveEl.textContent = '—';
      if (this.durationNominalEl) this.durationNominalEl.textContent = '—';
      if (this.windBadgeEl) {
        this.windBadgeEl.textContent = 'Awaiting Route';
        this.windBadgeEl.className = 'wind-badge';
      }
      if (this.windDescEl) {
        this.windDescEl.textContent = 'Select departure and arrival cities to calculate prevailing winds.';
      }
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
      sphericalMidpoint,
      isAntipodal,
      nominalFlightTimeHours,
      prevailingWindAdjustmentHours,
      effectiveFlightTimeHours
    } = geodesicResult;

    // 1. Total Distance Formatting
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
      : (distanceKm / 900);

    if (this.durationEffectiveEl) {
      this.durationEffectiveEl.textContent = formatDuration(effHours);
    }
    if (this.durationNominalEl) {
      if (prevailingWindAdjustmentHours > 0) {
        this.durationNominalEl.textContent = `Nominal: ${formatDuration(nomHours)}`;
      } else {
        this.durationNominalEl.textContent = 'Cruising speed: 900 km/h';
      }
    }

    // 3. Jet Stream Prevailing Wind Breakdown
    const windInfo = formatWindPenalty(prevailingWindAdjustmentHours, nomHours);
    if (this.windBadgeEl) {
      this.windBadgeEl.className = `wind-badge wind-badge-${windInfo.type}`;
      if (windInfo.type === 'headwind') {
        this.windBadgeEl.textContent = `+${windInfo.penaltyMinutes}m Headwind`;
      } else if (windInfo.type === 'tailwind') {
        this.windBadgeEl.textContent = 'Optimal Tailwind';
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
      if (isAntipodal) {
        this.antipodalBadge.style.display = 'block';
        this.antipodalBadge.classList.add('visible');
      } else {
        this.antipodalBadge.style.display = 'none';
        this.antipodalBadge.classList.remove('visible');
      }
    }
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
