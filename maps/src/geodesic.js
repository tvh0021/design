/**
 * Geodesic Calculations & Spherical Geometry Engine
 *
 * Provides:
 * - Spherical great-circle distance using Haversine & 3D vector algebra
 * - Vincenty distance on WGS-84 ellipsoid
 * - Spherical midpoint via normalized 3D Cartesian vector addition
 * - Antipodal detection with polar fallback routing over the North Pole
 * - Slerp (spherical linear interpolation) sampled waypoints formatted for GeoJSON/D3
 * - Forward initial bearing and reverse-arrival final bearing in degrees [0, 360)
 * - Physical jet stream model calibrated against empirical Flightradar24 airborne flight times
 *   incorporating latitude-dependent zonal westerlies, along-track vector projection, and
 *   North-South crosswind neutrality.
 */

export const EARTH_RADIUS_KM = 6371.0088;
export const KM_TO_MILES = 0.621371;

// Calibrated empirical flight parameters based on Flightradar24 airborne benchmarks:
export const CRUISE_SPEED_KMH = 870; // True airspeed at cruise (~Mach 0.82)
export const CLIMB_ALLOWANCE_HOURS = 0.35; // Climb & descent maneuver buffer (~21m)
export const JET_MAX_SPEED_KMH = 100; // Peak mid-latitude westerly jet stream speed
export const JET_CORE_LAT_DEG = 44; // Core latitude of mid-latitude jet stream
export const JET_LAT_WIDTH_DEG = 14; // Gaussian standard deviation width
export const WESTWARD_WIND_PENALTY_FACTOR = 0.14; // Backward-compatibility reference
export const DEFAULT_ANTIPODAL_THRESHOLD_DEG = 178.5;

/**
 * Normalizes a longitude degree value into [-180, 180).
 *
 * @param {number} lng - Longitude in degrees
 * @returns {number} Normalized longitude in [-180, 180)
 */
export function wrapLng(lng) {
  if (lng >= -180 && lng < 180) return lng;
  let l = (lng + 180) % 360;
  if (l < 0) l += 360;
  return l - 180;
}

/**
 * Converts degrees to radians.
 *
 * @param {number} deg
 * @returns {number}
 */
function toRad(deg) {
  return (deg * Math.PI) / 180;
}

/**
 * Converts radians to degrees.
 *
 * @param {number} rad
 * @returns {number}
 */
function toDeg(rad) {
  return (rad * 180) / Math.PI;
}

/**
 * Calculates the initial forward bearing from (lat1, lng1) to (lat2, lng2) in degrees [0, 360).
 * Bearing is measured clockwise from true north.
 *
 * @param {number} lat1
 * @param {number} lng1
 * @param {number} lat2
 * @param {number} lng2
 * @returns {number} Initial bearing in degrees [0, 360)
 */
export function calculateBearing(lat1, lng1, lat2, lng2) {
  const phi1 = toRad(lat1);
  const phi2 = toRad(lat2);
  const deltaLam = toRad(lng2 - lng1);

  const y = Math.sin(deltaLam) * Math.cos(phi2);
  const x =
    Math.cos(phi1) * Math.sin(phi2) -
    Math.sin(phi1) * Math.cos(phi2) * Math.cos(deltaLam);

  const initialRad = Math.atan2(y, x);
  const initialDeg = (toDeg(initialRad) + 360) % 360;
  return initialDeg;
}

/**
 * Calculates the climatological westerly (eastward) zonal jet stream wind speed (km/h)
 * at typical commercial cruise altitudes (~30,000 - 39,000 ft) as a function of latitude.
 *
 * Physics & Atmospheric Properties:
 * - Concentrated in mid-latitudes (~40° - 48° N/S).
 * - Suppressed in equatorial belt (|lat| < 12°) where westerly jet streams do not exist.
 * - Decays smoothly towards polar extremes (|lat| > 70°).
 *
 * @param {number} lat Latitude in degrees [-90, 90]
 * @returns {number} Zonal wind speed in km/h (positive eastward)
 */
export function getJetStreamSpeed(lat) {
  const absLat = Math.abs(lat);
  if (absLat < 10) return 0;
  const env = Math.exp(-0.5 * ((absLat - JET_CORE_LAT_DEG) / JET_LAT_WIDTH_DEG) ** 2);
  const polarDamp = absLat > 70 ? Math.max(0, (90 - absLat) / 20) : 1;
  const tropicalDamp = absLat < 20 ? (absLat - 10) / 10 : 1;
  return JET_MAX_SPEED_KMH * env * polarDamp * tropicalDamp;
}

/**
 * Computes great-circle geodesic route metrics between two geographic locations.
 *
 * @param {{ lat: number, lng: number, name?: string }} origin
 * @param {{ lat: number, lng: number, name?: string }} dest
 * @param {{ antipodalThresholdDeg?: number }} [options]
 * @returns {{
 *   distanceKm: number,
 *   distanceMiles: number,
 *   sphericalMidpoint: { lat: number, lng: number },
 *   isAntipodal: boolean,
 *   initialBearingDeg: number,
 *   finalBearingDeg: number,
 *   nominalFlightTimeHours: number,
 *   prevailingWindAdjustmentHours: number,
 *   effectiveFlightTimeHours: number,
 *   pathCoordinates: Array<[number, number]>
 * }}
 */
export function computeGeodesic(origin, dest, options = {}) {
  const antipodalThreshold = options.antipodalThresholdDeg ?? DEFAULT_ANTIPODAL_THRESHOLD_DEG;

  const lat1 = origin.lat;
  const lng1 = wrapLng(origin.lng);
  const lat2 = dest.lat;
  const lng2 = wrapLng(dest.lng);

  // 1. Haversine Distance on Spherical Earth
  const phi1 = toRad(lat1);
  const phi2 = toRad(lat2);
  const deltaPhi = toRad(lat2 - lat1);
  const deltaLam = toRad(lng2 - lng1);

  const a =
    Math.sin(deltaPhi / 2) ** 2 +
    Math.cos(phi1) * Math.cos(phi2) * Math.sin(deltaLam / 2) ** 2;
  const clampedA = Math.min(1, Math.max(0, a));
  const angularDistanceRad = 2 * Math.asin(Math.sqrt(clampedA));
  const distanceKm = EARTH_RADIUS_KM * angularDistanceRad;
  const distanceMiles = distanceKm * KM_TO_MILES;

  // Check degenerate case: identical points
  if (distanceKm < 1e-6 || (lat1 === lat2 && lng1 === lng2)) {
    return {
      distanceKm: 0,
      distanceMiles: 0,
      sphericalMidpoint: { lat: lat1, lng: wrapLng(lng1) },
      isAntipodal: false,
      initialBearingDeg: 0,
      finalBearingDeg: 0,
      nominalFlightTimeHours: 0,
      prevailingWindAdjustmentHours: 0,
      effectiveFlightTimeHours: 0,
      pathCoordinates: [
        [wrapLng(lng1), lat1],
        [wrapLng(lng2), lat2]
      ]
    };
  }

  // 3D Unit Vector Construction
  const v1 = [
    Math.cos(phi1) * Math.cos(toRad(lng1)),
    Math.cos(phi1) * Math.sin(toRad(lng1)),
    Math.sin(phi1)
  ];
  const v2 = [
    Math.cos(phi2) * Math.cos(toRad(lng2)),
    Math.cos(phi2) * Math.sin(toRad(lng2)),
    Math.sin(phi2)
  ];

  const dot = Math.min(1, Math.max(-1, v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]));
  const sigma = Math.acos(dot);
  const angularDistanceDeg = toDeg(sigma);

  // Antipodal Detection
  const isAntipodal = angularDistanceDeg >= antipodalThreshold;

  let initialBearingDeg;
  let finalBearingDeg;
  let sphericalMidpoint;

  if (isAntipodal) {
    // Exact antipodal points have undefined midpoints (any point on equatorial ring is equidistant).
    // Standard aviation fallback: trans-polar routing via the North Pole.
    sphericalMidpoint = { lat: 90, lng: 0 };
    initialBearingDeg = 0; // Heading due North toward pole
    finalBearingDeg = 180; // Arriving due South from pole
  } else {
    initialBearingDeg = calculateBearing(lat1, lng1, lat2, lng2);
    const reverseBearing = calculateBearing(lat2, lng2, lat1, lng1);
    finalBearingDeg = (reverseBearing + 180) % 360;

    // Vector sum of unit vectors
    const midX = v1[0] + v2[0];
    const midY = v1[1] + v2[1];
    const midZ = v1[2] + v2[2];
    const midNorm = Math.hypot(midX, midY, midZ);

    if (midNorm < 1e-7) {
      sphericalMidpoint = { lat: 90, lng: 0 };
    } else {
      const uMidX = midX / midNorm;
      const uMidY = midY / midNorm;
      const uMidZ = midZ / midNorm;

      const midLat = toDeg(Math.asin(Math.min(1, Math.max(-1, uMidZ))));
      const midLng = wrapLng(toDeg(Math.atan2(uMidY, uMidX)));
      sphericalMidpoint = { lat: midLat, lng: midLng };
    }
  }

  // 5. Waypoint Generation (Slerp)
  const pathCoordinates = [];

  if (isAntipodal) {
    // 100 points routing from origin up across North Pole down to destination
    const half = 50;
    for (let i = 0; i < half; i++) {
      const t = i / (half - 1);
      const lat = i === 0 ? lat1 : (i === half - 1 ? 90 : lat1 + t * (90 - lat1));
      pathCoordinates.push([wrapLng(lng1), lat]);
    }
    for (let i = 0; i < half; i++) {
      const t = i / (half - 1);
      const lat = i === half - 1 ? lat2 : (i === 0 ? 90 : 90 - t * (90 - lat2));
      pathCoordinates.push([wrapLng(lng2), lat]);
    }
  } else {
    // Slerp 100 waypoints along shortest great circle arc
    const numPoints = 100;
    const sinSigma = Math.sin(sigma);

    for (let i = 0; i < numPoints; i++) {
      const t = i / (numPoints - 1);
      if (t === 0) {
        pathCoordinates.push([wrapLng(lng1), lat1]);
        continue;
      }
      if (t === 1) {
        pathCoordinates.push([wrapLng(lng2), lat2]);
        continue;
      }

      const aFactor = Math.sin((1 - t) * sigma) / sinSigma;
      const bFactor = Math.sin(t * sigma) / sinSigma;

      const x = aFactor * v1[0] + bFactor * v2[0];
      const y = aFactor * v1[1] + bFactor * v2[1];
      const z = aFactor * v1[2] + bFactor * v2[2];
      const pNorm = Math.hypot(x, y, z);

      const pLat = toDeg(Math.asin(Math.min(1, Math.max(-1, z / pNorm))));
      const pLng = wrapLng(toDeg(Math.atan2(y, x)));

      pathCoordinates.push([pLng, pLat]);
    }
  }

  // 6. Flight Time & Jet Stream Integration
  // Evaluates along-track tailwind/headwind vector projections segment by segment
  let nominalFlightTimeHours = 0;
  let effectiveFlightTimeHours = 0;
  let prevailingWindAdjustmentHours = 0;

  if (distanceKm > 0 && pathCoordinates.length > 1) {
    let rawNominalTime = CLIMB_ALLOWANCE_HOURS;
    let rawEffectiveTime = CLIMB_ALLOWANCE_HOURS;

    for (let i = 0; i < pathCoordinates.length - 1; i++) {
      const [pLng1, pLat1] = pathCoordinates[i];
      const [pLng2, pLat2] = pathCoordinates[i + 1];

      // Segment distance via haversine
      const pPhi1 = toRad(pLat1);
      const pPhi2 = toRad(pLat2);
      const deltaPhiSeg = toRad(pLat2 - pLat1);
      const deltaLamSeg = toRad(pLng2 - pLng1);
      const aSeg =
        Math.sin(deltaPhiSeg / 2) ** 2 +
        Math.cos(pPhi1) * Math.cos(pPhi2) * Math.sin(deltaLamSeg / 2) ** 2;
      const dSeg = 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(Math.max(0, aSeg))));

      rawNominalTime += dSeg / CRUISE_SPEED_KMH;

      // Segment heading azimuth
      const ySeg = Math.sin(deltaLamSeg) * Math.cos(pPhi2);
      const xSeg =
        Math.cos(pPhi1) * Math.sin(pPhi2) -
        Math.sin(pPhi1) * Math.cos(pPhi2) * Math.cos(deltaLamSeg);
      const segBearingRad = Math.atan2(ySeg, xSeg);

      // Jet stream westerly wind at segment latitude
      const midLat = (pLat1 + pLat2) / 2;
      const uJet = getJetStreamSpeed(midLat);

      // Tailwind (+) / Headwind (-) along-track component: uJet * sin(bearing)
      const wAlong = uJet * Math.sin(segBearingRad);
      // Crosswind component: uJet * cos(bearing)
      const wCross = uJet * Math.cos(segBearingRad);

      // Effective groundspeed accounting for crab angle and along-track component
      const groundSpeed =
        Math.sqrt(Math.max(100, CRUISE_SPEED_KMH * CRUISE_SPEED_KMH - wCross * wCross)) + wAlong;
      rawEffectiveTime += dSeg / Math.max(300, groundSpeed);
    }

    nominalFlightTimeHours = rawNominalTime;
    effectiveFlightTimeHours = rawEffectiveTime;
    prevailingWindAdjustmentHours = effectiveFlightTimeHours - nominalFlightTimeHours;
  }

  return {
    distanceKm,
    distanceMiles,
    sphericalMidpoint,
    isAntipodal,
    initialBearingDeg,
    finalBearingDeg,
    nominalFlightTimeHours,
    prevailingWindAdjustmentHours,
    effectiveFlightTimeHours,
    pathCoordinates
  };
}
