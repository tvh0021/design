/**
 * Local Wi-Fi & LAN Remote Access Server
 * Serves the maps/ web application with zero external dependencies.
 * Automatically discovers LAN IPv4 addresses so you can test on your phone.
 */

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const ROOT_DIR = path.resolve(__dirname, '..');

const DEFAULT_PORT = parseInt(process.env.PORT || '8080', 10);
const HOST = '0.0.0.0';

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf'
};

function getLocalIpAddresses() {
  const interfaces = os.networkInterfaces();
  const addresses = [];
  for (const name of Object.keys(interfaces)) {
    for (const net of interfaces[name] || []) {
      if (net.family === 'IPv4' && !net.internal) {
        addresses.push({ iface: name, ip: net.address });
      }
    }
  }
  return addresses;
}

function handleRequest(req, res) {
  let reqPath = decodeURIComponent(req.url.split('?')[0]);
  if (reqPath === '/' || reqPath === '') {
    reqPath = '/index.html';
  }

  const safePath = path.normalize(reqPath).replace(/^(\.\.[/\\])+/, '');
  let filePath = path.join(ROOT_DIR, safePath);

  fs.stat(filePath, (err, stats) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end(`404 Not Found: ${reqPath}`);
      return;
    }

    if (stats.isDirectory()) {
      filePath = path.join(filePath, 'index.html');
    }

    const ext = path.extname(filePath).toLowerCase();
    const contentType = MIME_TYPES[ext] || 'application/octet-stream';

    res.writeHead(200, {
      'Content-Type': contentType,
      'Access-Control-Allow-Origin': '*',
      'Cache-Control': 'no-cache, must-revalidate'
    });

    const stream = fs.createReadStream(filePath);
    stream.on('error', (streamErr) => {
      res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end(`500 Internal Server Error: ${streamErr.message}`);
    });
    stream.pipe(res);
  });
}

function startServer(port) {
  const server = http.createServer(handleRequest);

  server.on('error', (err) => {
    if (err.code === 'EADDRINUSE') {
      console.log(`Port ${port} is currently in use. Trying port ${port + 1}...`);
      startServer(port + 1);
    } else {
      console.error('Server error:', err);
    }
  });

  server.listen(port, HOST, () => {
    const localIps = getLocalIpAddresses();
    const primaryIp = localIps.length > 0 ? localIps[0].ip : '127.0.0.1';

    console.log('\n' + '='.repeat(64));
    console.log('✈️   GEODESIC FLIGHT VISUALIZER — REMOTE PHONE ACCESS SERVER');
    console.log('='.repeat(64));
    console.log(`💻 On your Mac:     http://localhost:${port}`);
    console.log(`📱 On your Phone:   http://${primaryIp}:${port}`);
    if (localIps.length > 1) {
      for (let i = 1; i < localIps.length; i++) {
        console.log(`   (Alt network):   http://${localIps[i].ip}:${port}`);
      }
    }
    console.log('-'.repeat(64));
    console.log('💡 Quick phone connection steps:');
    console.log('   1. Ensure your phone is connected to the same Wi-Fi network.');
    console.log(`   2. Open Safari (iPhone) or Chrome (Android) and type:`);
    console.log(`      http://${primaryIp}:${port}`);
    console.log('   3. Enjoy touch-drag globe rotation & pinch-zoom flight navigation!');
    console.log('='.repeat(64));
    console.log('Press Ctrl+C to stop the server.\n');
  });
}

startServer(DEFAULT_PORT);
