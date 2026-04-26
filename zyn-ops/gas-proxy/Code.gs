/**
 * ZYN Empire — Discord Webhook Proxy (Google Apps Script)
 * ---------------------------------------------------------
 * Deploy this as a Web App (Execute as: Me, Access: Anyone) and use the
 * resulting /exec URL as GAS_PROXY_URL on the VM and in dashboard.html.
 *
 * Why a proxy?
 *   - Webhook URLs never appear in source, dashboard, or VM env files.
 *   - Single chokepoint for rate-limiting and STOP-signal enforcement.
 *   - Rotating a webhook means updating ONE Script Property, not 19 agents.
 *
 * POST body:
 *   { "channel": "noah", "username": "Noah", "content": "...", "embeds": [...] }
 *
 * Script Properties to set (Project Settings -> Script Properties):
 *   WEBHOOK_NOAH        = https://discord.com/api/webhooks/.../...
 *   WEBHOOK_MALIK       = ...
 *   WEBHOOK_SARA        = ...
 *   WEBHOOK_DRIFT       = ...
 *   WEBHOOK_HEALTH      = ...
 *   WEBHOOK_ALERTS      = ...
 *   WEBHOOK_OPS         = ...
 *   SHEET_ID            = <the spreadsheet id that holds CONTROL!A1>
 *   SHARED_SECRET       = <a long random string; agents send it as ?key=...>
 */

const RATE_LIMIT_PER_MIN = 30;     // per channel
const RATE_LIMIT_WINDOW_MS = 60000;
const CONTROL_CELL = 'CONTROL!A1';
const STOP_VALUE = 'STOP';

function doPost(e) {
  try {
    const props = PropertiesService.getScriptProperties();
    const expected = props.getProperty('SHARED_SECRET');
    const provided = (e.parameter && e.parameter.key) || '';
    if (!expected || provided !== expected) {
      return _json({ ok: false, error: 'unauthorized' }, 401);
    }

    if (_isStopped_(props)) {
      return _json({ ok: false, error: 'STOP signal active', dropped: true }, 200);
    }

    const body = JSON.parse(e.postData.contents || '{}');
    const channel = (body.channel || '').toLowerCase().replace(/[^a-z0-9_]/g, '');
    if (!channel) return _json({ ok: false, error: 'missing channel' }, 400);

    const propKey = 'WEBHOOK_' + channel.toUpperCase();
    const url = props.getProperty(propKey);
    if (!url) return _json({ ok: false, error: 'unknown channel: ' + channel }, 404);

    if (!_rateOk_(channel)) {
      return _json({ ok: false, error: 'rate limited', channel: channel }, 429);
    }

    const payload = {
      username: body.username || channel,
      content: (body.content || '').slice(0, 1900),
    };
    if (body.embeds) payload.embeds = body.embeds;
    if (body.avatar_url) payload.avatar_url = body.avatar_url;

    const resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
    return _json({ ok: resp.getResponseCode() < 300, code: resp.getResponseCode() }, 200);
  } catch (err) {
    return _json({ ok: false, error: String(err) }, 500);
  }
}

function doGet(e) {
  // Health check + STOP status (no secret required, no PII exposed)
  const props = PropertiesService.getScriptProperties();
  return _json({ ok: true, stopped: _isStopped_(props), ts: new Date().toISOString() }, 200);
}

function _isStopped_(props) {
  try {
    const id = props.getProperty('SHEET_ID');
    if (!id) return false;
    const ss = SpreadsheetApp.openById(id);
    const v = ss.getRange(CONTROL_CELL).getDisplayValue().trim().toUpperCase();
    return v === STOP_VALUE;
  } catch (err) {
    // If we cannot read the sheet, fail SAFE: do not block traffic on a sheet error.
    return false;
  }
}

function _rateOk_(channel) {
  const cache = CacheService.getScriptCache();
  const key = 'rl:' + channel;
  const raw = cache.get(key);
  const now = Date.now();
  let arr = raw ? JSON.parse(raw) : [];
  arr = arr.filter(t => now - t < RATE_LIMIT_WINDOW_MS);
  if (arr.length >= RATE_LIMIT_PER_MIN) return false;
  arr.push(now);
  cache.put(key, JSON.stringify(arr), 120);
  return true;
}

function _json(obj, code) {
  obj._status = code || 200;
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

