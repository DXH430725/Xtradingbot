/* server.js */
const express = require('express');
const path = require('path');
const app = express();

// 配置
const PORT = process.env.PORT || 10123;
const AUTH_TOKEN = process.env.TELEMETRY_TOKEN || ''; // 留空则不鉴权

// 内存保存
const bots = new Map();

app.use(express.json({ limit: '256kb' }));

// 上报入口：POST /ingest/:botName
app.post('/ingest/:botName', (req, res) => {
  if (AUTH_TOKEN && req.get('x-auth-token') !== AUTH_TOKEN) {
    return res.status(401).json({ status: 'unauthorized' });
  }
  const name = req.params.botName || 'unknown';
  const payload = req.body || {};
  bots.set(name, { name, payload, ts: Date.now() });
  res.json({ status: 'ok' });
});

// 状态接口：GET /api/status
app.get('/api/status', (_req, res) => {
  const now = Date.now();
  const arr = [];
  for (const b of bots.values()) {
    const intervalSec = Number(b.payload?.telemetry_interval_secs || 10);
    const online = now - b.ts < intervalSec * 2000; // 2个周期超时判离线
    arr.push({
      name: b.name,
      online,
      last_update_ms_ago: now - b.ts,
      payload: b.payload || {},
    });
  }
  res.set('Cache-Control', 'no-store');
  res.json({ now, bots: arr });
});

// 静态：同目录
app.use(express.static(__dirname, { cacheControl: false }));

// 主页：card.html
app.get('/', (_req, res) => {
  res.sendFile(path.join(__dirname, 'card.html'));
});

app.listen(PORT, () => {
  console.log(`Listening on http://0.0.0.0:${PORT}`);
  if (AUTH_TOKEN) console.log(`Auth token required (x-auth-token): ${AUTH_TOKEN}`);
});

