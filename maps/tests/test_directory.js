import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { CityDirectory } from '../src/directory.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const dataPath = path.resolve(__dirname, '../data/cities10k.json');
const curatedPath = path.resolve(__dirname, '../data/curated_locations.json');
const baseCityCount = 10000;
const curatedLocationCount = 111;
const datasetRecordCount = baseCityCount + curatedLocationCount;

console.log('--- Running CityDirectory Automated Test Suite ---');

// 1. Data File Verification
console.log('\n[Test 1] Verifying cities10k.json payload...');
assert(fs.existsSync(dataPath), 'cities10k.json must exist');
const stats = fs.statSync(dataPath);
const rawData = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
const curatedData = JSON.parse(fs.readFileSync(curatedPath, 'utf8'));

console.log(`  File size: ${stats.size} bytes (${(stats.size / 1024).toFixed(2)} KB)`);
console.log(`  Total records: ${rawData.length}`);
assert.equal(rawData.length, datasetRecordCount, 'Dataset must contain 10,000 cities plus 111 curated locations');
assert.equal(curatedData.length, curatedLocationCount, 'curated_locations.json must contain 111 locations');
assert.deepEqual(rawData.slice(baseCityCount), curatedData, 'Curated locations must follow the original 10,000 city records');
assert(Array.isArray(rawData[0]), 'First record must be an array');
assert.equal(rawData[0].length, 4, 'Records must be 4-element arrays: [name, country, lat, lng]');
assert.equal(rawData[baseCityCount][0], 'Longyearbyen', 'Curated locations must follow the original city records');

const normalizeName = (value) => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const baseNames = new Set(rawData.slice(0, baseCityCount).map((row) => normalizeName(row[0])));
const curatedNames = new Set();
for (const [name, , lat, lng] of curatedData) {
  assert(!curatedNames.has(normalizeName(name)), `Duplicate curated location name: ${name}`);
  assert(!baseNames.has(normalizeName(name)), `Curated location already exists in city list: ${name}`);
  curatedNames.add(normalizeName(name));
  assert(Number.isFinite(lat) && lat >= -90 && lat <= 90, `Invalid latitude for ${name}`);
  assert(Number.isFinite(lng) && lng >= -180 && lng <= 180, `Invalid longitude for ${name}`);
}

const distanceKm = (a, b) => {
  const radians = (degrees) => degrees * Math.PI / 180;
  const lat1 = radians(a[2]);
  const lat2 = radians(b[2]);
  const dLat = lat2 - lat1;
  const dLng = radians(b[3] - a[3]);
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * 6371.0088 * Math.asin(Math.sqrt(h));
};
let closestCuratedPair = Infinity;
for (let i = 0; i < curatedData.length; i += 1) {
  for (let j = i + 1; j < curatedData.length; j += 1) {
    closestCuratedPair = Math.min(closestCuratedPair, distanceKm(curatedData[i], curatedData[j]));
  }
}
assert(closestCuratedPair >= 20, `Curated locations must be at least 20 km apart; closest pair is ${closestCuratedPair.toFixed(2)} km`);
console.log('✓ Payload file verified.');

// 2. Directory Initialization & Load
console.log('\n[Test 2] Initializing CityDirectory and loading dataset...');
const directory = new CityDirectory();
directory.load(rawData);
assert.equal(directory.getAll().length, datasetRecordCount, 'getAll() must return all city and curated location records');
console.log('✓ Directory successfully loaded all records.');

// 3. Search "Tokyo"
console.log('\n[Test 3] Testing search("Tokyo")...');
const tokyoResults = directory.search('Tokyo');
assert(tokyoResults.length > 0, 'Must return at least 1 result for Tokyo');
const topTokyo = tokyoResults[0];
console.log('  Top result:', topTokyo);
assert.equal(topTokyo.name, 'Tokyo');
assert.equal(topTokyo.country, 'Japan');
assert.equal(topTokyo.id, 0);
assert.equal(topTokyo.lat, 35.6839);
assert.equal(topTokyo.lng, 139.7744);
console.log('✓ Tokyo search verified with exact coordinates and highest population rank.');

const longyearbyen = directory.search('Longyearbyen', 50)[0];
assert.equal(longyearbyen?.id, baseCityCount, 'Curated locations must be searchable in the directory');
assert.equal(longyearbyen.name, 'Longyearbyen');
assert.equal(longyearbyen.country, 'Norway');
const kilauea = directory.search('Kilauea', 50)[0];
assert.equal(kilauea?.name, 'Kīlauea', 'Search should normalize accents in curated location names');

// 4. Search "Paris"
console.log('\n[Test 4] Testing search("Paris")...');
const parisResults = directory.search('Paris');
assert(parisResults.length > 0, 'Must return at least 1 result for Paris');
const topParis = parisResults[0];
console.log('  Top result:', topParis);
assert.equal(topParis.name, 'Paris');
assert.equal(topParis.country, 'France');
assert.equal(topParis.lat, 48.8566);
assert.equal(topParis.lng, 2.3522);
console.log('✓ Paris, France prioritized correctly.');

// 5. Case-insensitivity & Diacritics
console.log('\n[Test 5] Testing case-insensitivity & diacritics...');
const lowerTokyo = directory.search('tokyo');
const upperTokyo = directory.search('TOKYO');
const mixedTokyo = directory.search('tOkYo');
assert.equal(lowerTokyo[0].id, topTokyo.id);
assert.equal(upperTokyo[0].id, topTokyo.id);
assert.equal(mixedTokyo[0].id, topTokyo.id);

const lowerParis = directory.search('paris');
const upperParis = directory.search('PARIS');
assert.equal(lowerParis[0].id, topParis.id);
assert.equal(upperParis[0].id, topParis.id);

// Accent handling (São Paulo -> Sao Paulo)
const saoPauloResults = directory.search('São Paulo');
assert(saoPauloResults.length > 0, 'Should match São Paulo with diacritic');
assert.equal(saoPauloResults[0].name, 'Sao Paulo');
console.log('✓ Case-insensitivity and diacritic tolerance verified.');

// 6. Empty Queries
console.log('\n[Test 6] Testing empty queries...');
assert.deepEqual(directory.search(''), []);
assert.deepEqual(directory.search('   '), []);
assert.deepEqual(directory.search(null), []);
assert.deepEqual(directory.search(undefined), []);
assert.deepEqual(directory.search(12345), []);
console.log('✓ Empty and invalid queries safely return empty array.');

// 7. Limit Constraints
console.log('\n[Test 7] Testing limit constraints...');
const defaultLimitResults = directory.search('San');
assert.equal(defaultLimitResults.length, 10, 'Default limit should be 10');

const limit5 = directory.search('San', 5);
assert.equal(limit5.length, 5, 'Limit of 5 should return 5 results');

const limit1 = directory.search('San', 1);
assert.equal(limit1.length, 1, 'Limit of 1 should return 1 result');

const limit0 = directory.search('San', 0);
assert.deepEqual(limit0, [], 'Limit of 0 should return empty array');

const limitNegative = directory.search('San', -3);
assert.deepEqual(limitNegative, [], 'Negative limit should return empty array');
console.log('✓ Limit constraints verified.');

// 8. Ranking: Prefix matches prioritized over substring matches
console.log('\n[Test 8] Testing prefix vs substring ranking...');
// Nishitokyo contains "tokyo", Tokyo starts with "tokyo"
const tokyoAll = directory.search('Tokyo', 20);
const tokyoIdx = tokyoAll.findIndex((c) => c.name === 'Tokyo');
const nishiIdx = tokyoAll.findIndex((c) => c.name === 'Nishitokyo');
assert(tokyoIdx !== -1, 'Tokyo must be in results');
if (nishiIdx !== -1) {
  assert(tokyoIdx < nishiIdx, 'Prefix match Tokyo must rank before substring match Nishitokyo');
}

// In general, all prefix matches should appear before any non-prefix matches
const sanResults = directory.search('san', 20);
let seenNonPrefix = false;
for (const city of sanResults) {
  const isPrefix = city.name.toLowerCase().startsWith('san');
  if (!isPrefix) {
    seenNonPrefix = true;
  } else if (seenNonPrefix) {
    assert.fail(`Found prefix match "${city.name}" after a non-prefix match`);
  }
}
console.log('✓ Ranking prioritization (prefix > substring, ordered by population) verified.');

// 9. getById Method
console.log('\n[Test 9] Testing getById...');
const city0 = directory.getById(0);
assert.equal(city0.name, 'Tokyo');
assert.equal(city0.id, 0);

const cityLast = directory.getById(9999);
assert(cityLast !== null);
assert.equal(cityLast.id, 9999);
assert.equal(directory.getById(10000).name, 'Longyearbyen');
assert.equal(directory.getById(10110).name, 'Chavín de Huántar');

assert.equal(directory.getById(-1), null);
assert.equal(directory.getById(10111), null);
assert.equal(directory.getById('0'), null);
assert.equal(directory.getById(null), null);
console.log('✓ getById lookup and boundary checks verified.');

// 10. Performance Benchmark
console.log('\n[Test 10] Running search performance benchmark (1,000 queries)...');
const queries = ['Tokyo', 'Paris', 'New York', 'San', 'London', 'Berlin', 'Cairo', 'Sydney', 'Beijing', 'Rio'];
const benchmarkIterations = 1000;
const start = performance.now();
for (let i = 0; i < benchmarkIterations; i++) {
  const q = queries[i % queries.length];
  directory.search(q, 10);
}
const elapsedMs = performance.now() - start;
const avgPerQueryMs = elapsedMs / benchmarkIterations;
console.log(`  Total time for ${benchmarkIterations} queries: ${elapsedMs.toFixed(2)} ms`);
console.log(`  Average search latency: ${(avgPerQueryMs * 1000).toFixed(2)} µs (${avgPerQueryMs.toFixed(4)} ms per query)`);
assert(avgPerQueryMs < 1.0, `Average search latency must be sub-millisecond (< 1.0 ms), got ${avgPerQueryMs.toFixed(4)} ms`);
console.log('✓ Search performance meets sub-millisecond criteria.');

console.log('\n========================================');
console.log('ALL TESTS PASSED SUCCESSFULLY! (10/10)');
console.log('========================================');
