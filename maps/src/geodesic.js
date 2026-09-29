/**
 * Geodesic flight path and spherical telemetry calculation module.
 *
 * Implements great-circle navigation mathematics:
 * - Haversine distance on WGS84 sphere (Earth radius 6371.0088 km)
 * - 3D Cartesian unit-vector midpoint calculation with antimeridian stability
 * - Antipodal detection with polar fallback routing over the North Pole
 * - Slerp (spherical linear interpolation) sampled waypoints formatted for GeoJSON/D3
 * - Forward initial bearing and reverse-arrival final bearing in degrees [0, 360)
 * - Commercial flight duration with prevailing westerly jet-stream wind penalty
 */

export const EARTH_RADIUS_KM = 6371.0088;
export const KM_TO_MILES = 0.621371;
export const CRUISE_SPEED_KMH = 900;
export const WESTWARD_WIND_PENALTY_FACTOR = 0.14;
export const DEFAULT_ANTIPODAL_THRESHOLD_DEG = 178.5;

/**
 * Normalizes an angle in degrees to the [-180, 180] range without floating-point degradation.
 * @param {number} lng
 * @returns {number}
 */
export function wrapLng(lng) {
  if (lng >= -180 && lng <= 180) {
    return lng;
  }
  let wrapped = lng;
  while (wrapped > 180) wrapped -= 360;
  while (wrapped < -180) wrapped += 360;
  return wrapped;
}

/**
 * Converts degrees to radians.
 * @param {number} deg
 * @returns {number}
 */
function toRad(deg) {
  return (deg * Math.PI) / 180;
}

/**
 * Converts radians to degrees.
 * @param {number} rad
 * @returns {number}
 */
function toDeg(rad) {
  return (rad * 180) / Math.PI;
}

/**
 * Calculates initial forward azimuth from (lat1, lng1) to (lat2, lng2) in degrees [0, 360).
 * @param {number} lat1
 * @param {number} lng1
 * @param {number} lat2
 * @param {number} lng2
 * @returns {number}
 */
export function calculateBearing(lat1, lng1, lat2, lng2) {
  const phi1 = toRad(lat1);
  const phi2 = toRad(lat2);
  const dlambda = toRad(lng2 - lng1);

  const y = Math.sin(dlambda) * Math.cos(phi2);
  const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dlambda);

  const bearing = toDeg(Math.atan2(y, x));
  return (bearing + 360) % 360;
}

/**
 * Computes the geodesic great-circle arc and telemetry between origin and destination.
 *
 * @param {{ lat: number, lng: number }} origin
 * @param {{ lat: number, lng: number }} destination
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
 *   pathCoordinates: [number, number][]
 * }}
 */
export function computeGeodesic(origin, destination, options = {}) {
  const antipodalThresholdDeg = options?.antipodalThresholdDeg ?? DEFAULT_ANTIPODAL_THRESHOLD_DEG;

  const lat1 = origin.lat;
  const lng1 = origin.lng;
  const lat2 = destination.lat;
  const lng2 = destination.lng;

  const phi1 = toRad(lat1);
  const phi2 = toRad(lat2);
  const lambda1 = toRad(lng1);
  const lambda2 = toRad(lng2);

  const dphi = phi2 - phi1;
  const dlambda = lambda2 - lambda1;

  // Haversine formula for angular separation sigma
  const sinHalfDphi = Math.sin(dphi / 2);
  const sinHalfDlambda = Math.sin(dlambda / 2);
  const a = sinHalfDphi * sinHalfDphi + Math.cos(phi1) * Math.cos(phi2) * sinHalfDlambda * sinHalfDlambda;
  const clampedA = Math.min(1, Math.max(0, a));
  const sigma = 2 * Math.atan2(Math.sqrt(clampedA), Math.sqrt(1 - clampedA));
  const angularDistanceDeg = toDeg(sigma);

  const distanceKm = EARTH_RADIUS_KM * sigma;
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

  // 3D Cartesian unit vectors
  const v1 = [
    Math.cos(phi1) * Math.cos(lambda1),
    Math.cos(phi1) * Math.sin(lambda1),
    Math.sin(phi1)
  ];
  const v2 = [
    Math.cos(phi2) * Math.cos(lambda2),
    Math.cos(phi2) * Math.sin(lambda2),
    Math.sin(phi2)
  ];

  const vm = [v1[0] + v2[0], v1[1] + v2[1], v1[2] + v2[2]];
  const vmNorm = Math.hypot(vm[0], vm[1], vm[2]);

  // Antipodal check
  const isAntipodal = angularDistanceDeg >= antipodalThresholdDeg || vmNorm < 1e-4;

  let sphericalMidpoint;
  const pathCoordinates = [];
  let initialBearingDeg;
  let finalBearingDeg;

  if (isAntipodal) {
    // Antipodal fallback: midpoint defaults to North Pole
    sphericalMidpoint = { lat: 90, lng: 0 };
    initialBearingDeg = 0; // Heading due North toward pole
    finalBearingDeg = 180; // Arriving due South from pole

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
    // Normalized 3D vector midpoint
    const vmid = [vm[0] / vmNorm, vm[1] / vmNorm, vm[2] / vmNorm];
    const latMid = toDeg(Math.asin(Math.min(1, Math.max(-1, vmid[2]))));
    const lngMid = wrapLng(toDeg(Math.atan2(vmid[1], vmid[0])));
    sphericalMidpoint = { lat: latMid, lng: lngMid };

    // Bearings
    initialBearingDeg = calculateBearing(lat1, lng1, lat2, lng2);
    const reverseBearing = calculateBearing(lat2, lng2, lat1, lng1);
    finalBearingDeg = (reverseBearing + 180) % 360;

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

  // Flight Time & Jet Stream Penalty
  const nominalFlightTimeHours = distanceKm / CRUISE_SPEED_KMH;
  const dlng = ((lng2 - lng1 + 540) % 360) - 180;
  let prevailingWindAdjustmentHours = 0;

  if (dlng < 0 && distanceKm > 0) {
    const meanLatRad = toRad((lat1 + lat2) / 2);
    const penaltyFactor = WESTWARD_WIND_PENALTY_FACTOR * Math.max(0, Math.cos(meanLatRad));
    prevailingWindAdjustmentHours = nominalFlightTimeHours * penaltyFactor;
  }

  const effectiveFlightTimeHours = nominalFlightTimeHours + prevailingWindAdjustmentHours;

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
