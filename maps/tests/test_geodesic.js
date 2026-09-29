import assert from 'node:assert/strict';
import {
  computeGeodesic,
  wrapLng,
  calculateBearing,
  EARTH_RADIUS_KM,
  KM_TO_MILES
} from '../src/geodesic.js';

console.log('--- Running Geodesic Engine Test Suite ---\n');

let totalTests = 0;
let passedTests = 0;

function runTest(name, fn) {
  totalTests++;
  try {
    fn();
    console.log(`\u2714 PASS: ${name}`);
    passedTests++;
  } catch (err) {
    console.error(`\u2716 FAIL: ${name}`);
    console.error(err);
    process.exitCode = 1;
  }
}

// 1. Test Paris -> Fairbanks
runTest('Paris -> Fairbanks polar great-circle route', () => {
  const paris = { lat: 48.8566, lng: 2.3522 };
  const fairbanks = { lat: 64.8378, lng: -147.7164 };

  const result = computeGeodesic(paris, fairbanks);

  // Reference distance check (~7110.8 km, ~4418.5 miles)
  const expectedDistanceKm = 7110.82;
  const toleranceKm = 10;
  assert(
    Math.abs(result.distanceKm - expectedDistanceKm) < toleranceKm,
    `Distance ${result.distanceKm} km should be close to reference ${expectedDistanceKm} km`
  );

  assert.strictEqual(
    result.distanceMiles,
    result.distanceKm * KM_TO_MILES,
    'Miles should equal km * 0.621371'
  );

  // Path coordinates
  assert.strictEqual(result.pathCoordinates.length, 100, 'Should have exactly 100 coordinates');
  assert.deepStrictEqual(
    result.pathCoordinates[0],
    [paris.lng, paris.lat],
    'First point should match origin [lng, lat]'
  );
  assert.deepStrictEqual(
    result.pathCoordinates[99],
    [fairbanks.lng, fairbanks.lat],
    'Last point should match destination [lng, lat]'
  );

  // Check no NaN values
  for (let i = 0; i < result.pathCoordinates.length; i++) {
    const [lng, lat] = result.pathCoordinates[i];
    assert(!isNaN(lng) && !isNaN(lat), `Coordinate at index ${i} must not contain NaN`);
    assert(lng >= -180 && lng <= 180, `Longitude ${lng} must be in [-180, 180]`);
    assert(lat >= -90 && lat <= 90, `Latitude ${lat} must be in [-90, 90]`);
  }

  // Midpoint should be in the high Arctic (lat > 70)
  assert(result.sphericalMidpoint.lat > 70, 'Midpoint should pass near Arctic latitudes');
  assert(!result.isAntipodal, 'Paris -> Fairbanks is not antipodal');

  // Flight time check
  assert(
    Math.abs(result.nominalFlightTimeHours - result.distanceKm / 900) < 1e-6,
    'Nominal flight time should be distanceKm / 900'
  );
  assert(result.effectiveFlightTimeHours >= result.nominalFlightTimeHours);
});

// 2. Test New York -> London vs London -> New York (westerly wind penalty)
runTest('New York <-> London jet-stream wind penalty comparison', () => {
  const newYork = { lat: 40.7128, lng: -74.0060 };
  const london = { lat: 51.5074, lng: -0.1278 };

  const nyToLondon = computeGeodesic(newYork, london);
  const londonToNy = computeGeodesic(london, newYork);

  // Distances should be symmetric
  assert(
    Math.abs(nyToLondon.distanceKm - londonToNy.distanceKm) < 1e-4,
    'Distance must be symmetric'
  );

  // Eastbound flight (NY -> London) has positive dlng: zero wind penalty
  assert.strictEqual(
    nyToLondon.prevailingWindAdjustmentHours,
    0,
    'Eastbound flight should incur 0 wind penalty'
  );
  assert.strictEqual(
    nyToLondon.effectiveFlightTimeHours,
    nyToLondon.nominalFlightTimeHours,
    'Eastbound effective flight time should equal nominal'
  );

  // Westbound flight (London -> NY) has negative dlng: wind penalty applied
  assert(
    londonToNy.prevailingWindAdjustmentHours > 0,
    'Westbound flight must incur positive wind adjustment'
  );
  assert(
    londonToNy.effectiveFlightTimeHours > londonToNy.nominalFlightTimeHours,
    'Westbound effective flight time must exceed nominal'
  );

  // Verify westbound takes longer than eastbound
  assert(
    londonToNy.effectiveFlightTimeHours > nyToLondon.effectiveFlightTimeHours,
    'Westbound flight must take strictly longer than eastbound flight due to wind penalty'
  );

  // Ratio should be around 1.097 (+9.7% wind penalty, well within the 8%-14% range)
  const ratio = londonToNy.effectiveFlightTimeHours / nyToLondon.effectiveFlightTimeHours;
  assert(
    ratio >= 1.05 && ratio <= 1.15,
    `Westbound flight time ratio ${ratio} should be ~1.10 (between 1.05 and 1.15)`
  );
});

// 3. Test Antimeridian crossing (Tokyo -> San Francisco)
runTest('Antimeridian crossing (Tokyo -> San Francisco)', () => {
  const tokyo = { lat: 35.6762, lng: 139.6503 };
  const sfo = { lat: 37.7749, lng: -122.4194 };

  const result = computeGeodesic(tokyo, sfo);

  // Shortest great-circle distance is ~8274 km (not > 30,000 km)
  assert(
    result.distanceKm > 8000 && result.distanceKm < 8500,
    `Distance across Pacific should be ~8275 km, got ${result.distanceKm}`
  );

  // Spherical midpoint should be in the North Pacific near Aleutians (-172.28°, 48.65°)
  assert(
    result.sphericalMidpoint.lng < -160 && result.sphericalMidpoint.lng > -180,
    `Midpoint lng ${result.sphericalMidpoint.lng} must be in North Pacific [-180, -160]`
  );
  assert(
    result.sphericalMidpoint.lat > 45 && result.sphericalMidpoint.lat < 55,
    `Midpoint lat ${result.sphericalMidpoint.lat} must be around ~48.6°`
  );

  // Verify coordinates are well-formed and wrap correctly
  assert.strictEqual(result.pathCoordinates.length, 100);
  for (const [lng, lat] of result.pathCoordinates) {
    assert(!isNaN(lng) && !isNaN(lat), 'Coordinates must not be NaN');
    assert(lng >= -180 && lng <= 180, `Longitude ${lng} must be in [-180, 180]`);
    assert(lat >= -90 && lat <= 90, `Latitude ${lat} must be in [-90, 90]`);
  }

  // Check that the trajectory crosses the 180 antimeridian
  const hasPositiveLng = result.pathCoordinates.some(([lng]) => lng > 130);
  const hasNegativeLng = result.pathCoordinates.some(([lng]) => lng < -120);
  assert(hasPositiveLng && hasNegativeLng, 'Route must cross from positive to negative longitudes');
});

// 4. Test Antipodal pair (Madrid and Wellington)
runTest('Antipodal pair (Madrid and Wellington)', () => {
  const madrid = { lat: 40.4168, lng: -3.7038 };
  const wellington = { lat: -41.2865, lng: 174.7762 };

  const result = computeGeodesic(madrid, wellington);

  // Antipodal status
  assert.strictEqual(result.isAntipodal, true, 'isAntipodal must be true for Madrid-Wellington');

  // Midpoint fallback to North Pole
  assert.deepStrictEqual(
    result.sphericalMidpoint,
    { lat: 90, lng: 0 },
    'Antipodal midpoint must default to North Pole { lat: 90, lng: 0 }'
  );

  // Coordinates check
  assert.strictEqual(result.pathCoordinates.length, 100, 'Should have 100 coordinates');
  assert.deepStrictEqual(result.pathCoordinates[0], [madrid.lng, madrid.lat]);
  assert.deepStrictEqual(result.pathCoordinates[99], [wellington.lng, wellington.lat]);

  // Coordinates must contain NO NaN values
  for (let i = 0; i < result.pathCoordinates.length; i++) {
    const [lng, lat] = result.pathCoordinates[i];
    assert(!isNaN(lng), `Point ${i} longitude is NaN`);
    assert(!isNaN(lat), `Point ${i} latitude is NaN`);
    assert(lng >= -180 && lng <= 180, `Longitude ${lng} out of range`);
    assert(lat >= -90 && lat <= 90, `Latitude ${lat} out of range`);
  }

  // Trajectory routes up across the North Pole (lat = 90)
  const hitsNorthPole = result.pathCoordinates.some(([, lat]) => lat === 90);
  assert(hitsNorthPole, 'Antipodal route must explicitly pass through North Pole (lat = 90)');

  // Initial and final bearings for polar path
  assert.strictEqual(result.initialBearingDeg, 0, 'Initial bearing should be 0 (due North)');
  assert.strictEqual(result.finalBearingDeg, 180, 'Final bearing should be 180 (due South)');
});

// 5. Test Identical point: distance = 0
runTest('Identical origin and destination (degenerate case)', () => {
  const point = { lat: 37.7749, lng: -122.4194 };

  const result = computeGeodesic(point, point);

  assert.strictEqual(result.distanceKm, 0, 'distanceKm must be 0');
  assert.strictEqual(result.distanceMiles, 0, 'distanceMiles must be 0');
  assert.deepStrictEqual(
    result.sphericalMidpoint,
    { lat: point.lat, lng: point.lng },
    'Midpoint must equal the point itself'
  );
  assert.strictEqual(result.isAntipodal, false, 'isAntipodal must be false');
  assert.strictEqual(result.initialBearingDeg, 0, 'initialBearingDeg should be 0');
  assert.strictEqual(result.finalBearingDeg, 0, 'finalBearingDeg should be 0');
  assert.strictEqual(result.nominalFlightTimeHours, 0, 'nominalFlightTimeHours must be 0');
  assert.strictEqual(result.prevailingWindAdjustmentHours, 0, 'prevailingWindAdjustmentHours must be 0');
  assert.strictEqual(result.effectiveFlightTimeHours, 0, 'effectiveFlightTimeHours must be 0');

  assert.strictEqual(result.pathCoordinates.length, 2, 'Degenerate path must have 2 identical points');
  assert.deepStrictEqual(result.pathCoordinates[0], [point.lng, point.lat]);
  assert.deepStrictEqual(result.pathCoordinates[1], [point.lng, point.lat]);
});

// 6. Test Exact Antipodal Points (0, 0) and (0, 180)
runTest('Exact antipodal points (0, 0) and (0, 180)', () => {
  const p1 = { lat: 0, lng: 0 };
  const p2 = { lat: 0, lng: 180 };

  const result = computeGeodesic(p1, p2);

  assert.strictEqual(result.isAntipodal, true, 'isAntipodal must be true');
  assert.deepStrictEqual(result.sphericalMidpoint, { lat: 90, lng: 0 });
  assert.strictEqual(result.pathCoordinates.length, 100);

  // Maximum Earth half-circumference: pi * R = 20015.086 km
  const expectedHalfCircumference = Math.PI * EARTH_RADIUS_KM;
  assert(
    Math.abs(result.distanceKm - expectedHalfCircumference) < 1,
    `Distance ${result.distanceKm} should equal half circumference ${expectedHalfCircumference}`
  );

  for (const [lng, lat] of result.pathCoordinates) {
    assert(!isNaN(lng) && !isNaN(lat), 'No NaN in coordinates');
  }
});

// 7. Test helper functions: wrapLng and calculateBearing
runTest('Helper functions: wrapLng and calculateBearing', () => {
  assert.strictEqual(wrapLng(0), 0);
  assert(wrapLng(180) === 180 || wrapLng(180) === -180);
  assert(wrapLng(-180) === 180 || wrapLng(-180) === -180);
  assert.strictEqual(wrapLng(190), -170);
  assert.strictEqual(wrapLng(-190), 170);
  assert.strictEqual(wrapLng(360), 0);

  // Cardinal directions along equator / prime meridian
  assert.strictEqual(calculateBearing(0, 0, 0, 10), 90, 'East along equator');
  assert.strictEqual(calculateBearing(0, 10, 0, 0), 270, 'West along equator');
  assert.strictEqual(calculateBearing(0, 0, 10, 0), 0, 'North along prime meridian');
  assert.strictEqual(calculateBearing(10, 0, 0, 0), 180, 'South along prime meridian');
});

// 8. Test Southern Hemisphere route: Sydney to Santiago
runTest('Southern Hemisphere route: Sydney -> Santiago', () => {
  const sydney = { lat: -33.8688, lng: 151.2093 };
  const santiago = { lat: -33.4489, lng: -70.6693 };

  const result = computeGeodesic(sydney, santiago);

  // Great circle arcs down towards Antarctica in Southern Ocean
  assert(result.sphericalMidpoint.lat < -50, 'Midpoint should bend towards high southern latitudes');
  assert(result.distanceKm > 11000 && result.distanceKm < 12000, 'Distance should be ~11340 km');
  assert.strictEqual(result.pathCoordinates.length, 100);
  assert.strictEqual(result.isAntipodal, false);
});

console.log(`\n========================================`);
console.log(`Summary: ${passedTests}/${totalTests} tests passed.`);
console.log(`========================================\n`);

if (passedTests !== totalTests) {
  process.exit(1);
}
