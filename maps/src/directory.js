/**
 * CityDirectory - High-performance search and retrieval for curated city records.
 *
 * Implements sub-millisecond search autocomplete across top global cities,
 * prioritizing prefix matches before substring matches, preserving population
 * ranking.
 */

function normalizeText(text) {
  if (typeof text !== 'string') return '';
  return text
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .trim();
}

export class CityDirectory {
  constructor() {
    this.cities = [];
    this.records = [];
  }

  /**
   * Loads an array of city tuples: [name, country, lat, lng]
   * or city objects: { name, country, lat, lng }
   * The input is expected to be pre-sorted in descending population order.
   *
   * @param {Array<Array|Object>} data
   */
  load(data) {
    if (!Array.isArray(data)) {
      this.cities = [];
      this.records = [];
      return;
    }

    const n = data.length;
    this.records = new Array(n);
    this.cities = new Array(n);

    for (let id = 0; id < n; id++) {
      const item = data[id];
      let name, country, lat, lng;

      if (Array.isArray(item)) {
        name = item[0] ?? '';
        country = item[1] ?? '';
        lat = item[2] ?? 0;
        lng = item[3] ?? 0;
      } else if (item && typeof item === 'object') {
        name = item.name ?? '';
        country = item.country ?? '';
        lat = item.lat ?? 0;
        lng = item.lng ?? 0;
      } else {
        name = '';
        country = '';
        lat = 0;
        lng = 0;
      }

      // Public immutable representation
      const publicCity = Object.freeze({
        id,
        name,
        country,
        lat,
        lng
      });
      this.records[id] = publicCity;

      // Internal optimized search record
      const nameNorm = normalizeText(name);
      const countryNorm = normalizeText(country);
      const fullNameNorm = `${nameNorm}, ${countryNorm}`;

      this.cities[id] = {
        city: publicCity,
        nameNorm,
        countryNorm,
        fullNameNorm
      };
    }
  }

  /**
   * Retrieves a city by its sequential ID (0 to N-1).
   *
   * @param {number} id
   * @returns {Object|null} {id, name, country, lat, lng} or null
   */
  getById(id) {
    if (typeof id !== 'number' || !Number.isInteger(id) || id < 0 || id >= this.records.length) {
      return null;
    }
    return this.records[id] ?? null;
  }

  /**
   * Returns all loaded city records.
   *
   * @returns {Array<Object>}
   */
  getAll() {
    return this.records.slice();
  }

  /**
   * Searches cities by name (or country), prioritizing prefix matches
   * over substring matches, maintaining descending population rank order.
   *
   * @param {string} query
   * @param {number} [limit=10]
   * @returns {Array<Object>} Array of {id, name, country, lat, lng}
   */
  search(query, limit = 10) {
    if (typeof query !== 'string' || !query.trim() || limit <= 0) {
      return [];
    }

    const q = normalizeText(query);
    if (!q) {
      return [];
    }

    const prefixMatches = [];
    const substringMatches = [];
    const fallbackMatches = [];

    const total = this.cities.length;
    for (let i = 0; i < total; i++) {
      const entry = this.cities[i];
      const { city, nameNorm, countryNorm, fullNameNorm } = entry;

      if (nameNorm.startsWith(q)) {
        prefixMatches.push(city);
        if (prefixMatches.length >= limit) {
          return prefixMatches;
        }
      } else if (nameNorm.includes(q)) {
        if (prefixMatches.length + substringMatches.length < limit * 2) {
          substringMatches.push(city);
        }
      } else if (fullNameNorm.includes(q) || countryNorm.startsWith(q)) {
        if (prefixMatches.length + substringMatches.length + fallbackMatches.length < limit * 2) {
          fallbackMatches.push(city);
        }
      }
    }

    const combined = prefixMatches.concat(substringMatches, fallbackMatches);
    return combined.slice(0, limit);
  }
}
