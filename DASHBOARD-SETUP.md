# ZYN Empire Dashboard — Setup & Operations Guide

**Last updated:** April 27, 2026
**Owner:** Zion Marshall (`coachmarshall72@yahoo.com`)
**Repo:** [`ancient1zion/zynsl-website`](https://github.com/ancient1zion/zynsl-website)

---

## What this dashboard does

Live operations view for the ZYN Empire — 19 autonomous agents pushing real-time numbers (pipeline, leads, agent status, trading P&L, contracts, outreach) to a single page that auto-updates without manual refresh. Designed so you (and only you) can see the state of the business at a glance, anywhere.

**Live URL (current):** https://ancient1zion.github.io/zynsl-website/dashboard.html
**Target URL (private, post-Cloudflare):** https://empire.zynsupplyandlogistics.com

---

## Architecture

```
┌────────────────────────────┐         ┌──────────────────────────┐
│  Agents (19)               │ writes  │  Google Sheet            │
│  Sara, Caleb, Ruth, Adam,  │ ──────▶ │  ID: 1WHN438m...iY1d_VR  │
│  Noah, etc.                │         │  Tabs: CRM, STATS        │
└────────────────────────────┘         └──────────┬───────────────┘
                                                  │ reads
                                                  ▼
                                       ┌──────────────────────────┐
                                       │  Apps Script Web App     │
                                       │  /macros/s/AKfyc.../exec │
                                       │  Returns aggregated JSON │
                                       └──────────┬───────────────┘
                                                  │ polls every 30s
                                                  ▼
┌────────────────────────────┐         ┌──────────────────────────┐
│  GitHub Pages              │ serves  │  dashboard.html          │
│  ancient1zion.github.io/   │ ──────▶ │  19 data-zyn elements    │
│  zynsl-website/            │         │  Auto-bind to JSON       │
└────────────────────────────┘         └──────────────────────────┘
```

**Key principle:** agents don't talk to the dashboard directly. They write to the Sheet (which they already do). The dashboard reads from the Sheet via Apps Script. Decoupled, no agent code changes needed for new dashboard features.

---

## Components

### 1. `dashboard.html` — the page itself

Lives in the repo root. Contains:

- **19 stat elements tagged with `data-zyn="<key>"` attributes.** These get live-updated every 30 seconds. Full key list below.
- **An auto-update `<script>` block** at the bottom that polls two URLs:
  - `version.json` every 60s — triggers full page reload when version changes (deploy mechanism)
  - The Apps Script `/exec` URL every 30s — updates stat numbers in-place without reload
- **Cache-busting `<meta>` tags** in the `<head>` — prevents GitHub Pages from serving stale HTML.

### 2. `version.json` — deploy trigger

```json
{
  "version": "v13.0",
  "deployed_at": "2026-04-27T15:38:23.295Z",
  "notes": "Bump version field on every dashboard change to force all agent browsers to auto-reload within 60 seconds."
}
```

**To trigger a fleet-wide reload:** bump the `version` field, commit. Within 60 seconds, every open dashboard auto-reloads.

### 3. Apps Script Web App — the data layer

**Project:** `ZYN Dashboard Stats Endpoint`
**URL:** `https://script.google.com/macros/s/AKfycbwDDC1Of6v1CVmE8lY21e0SbEDSqMTITLfLXOReoIsZ4JaqRB-MmJwEO1pm0PAi4Wcf8w/exec`
**Execute as:** `coachmarshall72@yahoo.com`
**Access:** Anyone (currently — will be locked down behind Cloudflare Access)

**What it does:**
- Reads from Google Sheet `1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk`
- Aggregates lead counts from the `CRM` tab (or `Leads` tab as fallback)
- Reads manual overrides from the `STATS` tab (key/value pairs in columns A and B)
- Returns a single JSON blob with pipeline, leads, agents, outreach, trading, contracts data

**Source code:** see "Apps Script source" section at the bottom of this doc.

### 4. Google Sheet — the data backbone

**Sheet ID:** `1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk`
**URL:** https://docs.google.com/spreadsheets/d/1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk/edit

**Tabs the dashboard reads:**

- **`CRM`** (or `Leads` as fallback) — live lead data. Headers expected: `Stage`, `Priority`, `Value`. Variants accepted: `Status`/`Lead Status` for stage; `Temp`/`Temperature` for priority; `Deal Value`/`Amount`/`Revenue` for value.
- **`STATS`** — manual overrides. Column A = dot-notation key (e.g. `pipeline.consulting_arr`), Column B = value. Anything in this tab overrides the calculated stats. Useful for projections, hardcoded numbers, or stats that don't come from the CRM.

---

## The 19 data-zyn keys (live-bound elements)

| Where on dashboard | Key | What it shows |
|---|---|---|
| Top KPI strip | `outreach.emails_sent_today` | Today's email count from Sara's outreach |
| Top KPI strip | `leads.hot` | Hot leads from CRM (`Priority = HOT`) |
| Top KPI strip | `pipeline.won_revenue` | Sum of `Value` for `Stage = Won` rows |
| Top KPI strip | `contracts.drafting` | Count of contracts being drafted |
| Top KPI strip | `pipeline.total_pipeline` | Combined pipeline total |
| Top KPI strip | `agents.vm_uptime_pct` | GCP VM uptime |
| Top KPI strip | `agents.active` | Active agent count |
| Top KPI strip | `agents.webhooks_live` | Live webhook count |
| CRM section | `leads.total` | Total leads in CRM |
| CRM section | `leads.hot` | (same as top — both update) |
| CRM section | `leads.contacted` | Leads at `Contacted` stage |
| CRM section | `leads.proposals` | Leads at `Proposal` stage |
| CRM section | `leads.negotiating` | Leads at `Negotiating` stage |
| CRM section | `leads.won` | Leads at `Won` stage |
| CRM section | `pipeline.won_revenue` | (same as top — both update) |
| Financial bar | `pipeline.consulting_arr` | Consulting ARR |
| Financial bar | `pipeline.ai_systems` | AI systems revenue |
| Financial bar | `pipeline.federal_pipeline` | Federal contract pipeline |
| Financial bar | `pipeline.total_pipeline` | (same as top — both update) |

**Three keys appear twice** (`leads.hot`, `pipeline.won_revenue`, `pipeline.total_pipeline`) — both elements update simultaneously.

---

## How to deploy a dashboard change

1. Edit `dashboard.html` in GitHub
2. Bump `CURRENT_VERSION` inside the script block (e.g. `'v13.0'` → `'v13.1'`)
3. Bump `version.json` to match: `{ "version": "v13.1" }`
4. Commit

**Within 60 seconds, every open dashboard auto-reloads.** No manual refresh needed.

---

## How to update a stat number

Three ways, in order of preference:

### A. Update the CRM tab (for lead-related stats)

Just edit the Google Sheet. Within 30 seconds the dashboard reflects it. This is what your agents do automatically.

### B. Use the STATS override tab (for hardcoded numbers)

Open the Sheet, go to `STATS` tab. Column A = key, Column B = value. Examples:

| A (key) | B (value) |
|---|---|
| `pipeline.consulting_arr` | 450000 |
| `agents.vm_uptime_pct` | 99.95 |
| `trading.caleb_pnl_today` | 1247 |

Anything in this tab overrides what the script calculates from CRM. Save → wait 30s → dashboard updates.

### C. Have an agent push it programmatically

Use `zyn-stats-pusher.gs` (from earlier in the project). One function call: `zynPushStats({trading: {caleb_pnl_today: 1247}})`. Writes directly to the data layer. Useful for agents already running in Google Apps Script.

---

## How to wire a new dashboard number to live data

1. Find the element in `dashboard.html` you want to make live
2. Add `data-zyn="<key>"` attribute, where `<key>` is dot-notation (e.g. `outreach.emails_sent_today`)
3. Make sure the Apps Script returns that key in the JSON (extend `buildStats()` in the script if needed)
4. Commit + bump version
5. Done

---

## Privacy / Access (in progress as of this doc)

**Current state:** dashboard is public at `ancient1zion.github.io/zynsl-website/dashboard.html`. Anyone with URL can view.

**Target state:** dashboard at `https://empire.zynsupplyandlogistics.com` behind Cloudflare Access, only `coachmarshall72@yahoo.com` can view (one-time-PIN to that email address).

**Stack:**
- Domain `zynsupplyandlogistics.com` registered at GoDaddy
- DNS managed by Cloudflare (free plan)
- Hosting moves from GitHub Pages → Cloudflare Pages (project: `zyn-empire-dashboard`)
- Auth via Cloudflare Zero Trust → Access policy "Only Zion"

**Status:** Cloudflare zone `zynsupplyandlogistics.com` waiting on activation. DNSSEC was disabled at GoDaddy to clear a delegation conflict; full DNS propagation is complete but Cloudflare's verifier hasn't flipped the zone to Active yet. Community support thread open at:

https://community.cloudflare.com/t/zone-stuck-in-pending-2-hours-despite-correct-nameservers-no-dnssec-full-propaga/923580

---

## Troubleshooting

### Dashboard shows old/stale numbers
- Check `version.json` and `CURRENT_VERSION` in dashboard.html match. If not, bump both.
- Check the Apps Script URL is reachable: open `https://script.google.com/macros/s/AKfycbwDDC1Of6v1CVmE8lY21e0SbEDSqMTITLfLXOReoIsZ4JaqRB-MmJwEO1pm0PAi4Wcf8w/exec` in a browser. Should return JSON.
- Check the Sheet is reachable: the script's "Execute as" account (`coachmarshall72@yahoo.com`) must have read access to the Sheet.

### A specific stat number is stuck at 0
- That key isn't in the JSON (open the `/exec` URL in a browser to check)
- Or the dashboard element doesn't have the right `data-zyn` attribute (inspect element to verify)
- Or the CRM tab column headers don't match what the script looks for (`Stage`, `Priority`, `Value` and known variants)

### Agents push to Sheet but numbers don't update
- The script polls fresh on each request (no caching) so it should be instant. If not, check the Sheet structure changed (column moved, header renamed)

### Dashboard doesn't auto-reload after bumping version
- Confirm both files were committed: `dashboard.html` (with new `CURRENT_VERSION`) AND `version.json` (with new `version`)
- GitHub Pages can take up to ~90 seconds to deploy. Wait, then check.
- DevTools → Network tab → filter by "version.json" → confirm it's being polled and returning the new value

---

## Files in this repo relevant to the dashboard

- `dashboard.html` — the dashboard itself
- `version.json` — version trigger for auto-reload
- `stats.json` — **deprecated, will be deleted** once Cloudflare Pages migration is complete (data now comes from Apps Script)
- `index.html` — repo landing page (not the dashboard)
- `DASHBOARD-SETUP.md` — this file

---

## Apps Script source (for reference / restoration)

If the Apps Script project is ever lost, recreate from this. Save in a new project, bind to the Sheet ID, deploy as Web App with "Execute as: Me, Access: Anyone."

```javascript
const SHEET_ID = '1WHN438mjORT4HnGiXapWv78uomVy6KMhHsl0llxaeQk';

function doGet(e) {
  const stats = {
    updated_at: new Date().toISOString(),
    pipeline: {
      consulting_arr: 420000,
      ai_systems: 600000,
      federal_pipeline: 592000,
      total_pipeline: 1612000,
      won_revenue: 0
    },
    leads: { total: 0, hot: 0, contacted: 0, proposals: 0, negotiating: 0, won: 0 },
    agents: { active: 19, webhooks_live: 19, vm_uptime_pct: 99.9 },
    outreach: { emails_sent_today: 0, emails_sent_total: 0 },
    trading: { caleb_pnl_today: 0, caleb_pnl_total: 0, trades_today: 0 },
    contracts: { drafting: 1, submitted: 0, won: 0 }
  };

  try {
    const ss = SpreadsheetApp.openById(SHEET_ID);

    // Live counts from CRM tab
    const crm = ss.getSheetByName('CRM') || ss.getSheetByName('Leads');
    if (crm && crm.getLastRow() > 1) {
      const headers = crm.getRange(1, 1, 1, crm.getLastColumn()).getValues()[0]
        .map(h => String(h).toLowerCase().trim());
      const data = crm.getRange(2, 1, crm.getLastRow() - 1, crm.getLastColumn()).getValues();

      const colIdx = (names) => {
        for (const n of names) {
          const i = headers.indexOf(n);
          if (i !== -1) return i;
        }
        return -1;
      };
      const stageCol    = colIdx(['stage', 'status', 'lead status']);
      const priorityCol = colIdx(['priority', 'temp', 'temperature']);
      const valueCol    = colIdx(['value', 'deal value', 'amount', 'revenue']);

      stats.leads.total = data.length;

      if (stageCol !== -1) {
        for (const row of data) {
          const stage = String(row[stageCol] || '').toLowerCase();
          if (/contact/.test(stage))   stats.leads.contacted++;
          if (/proposal/.test(stage))  stats.leads.proposals++;
          if (/negotiat/.test(stage))  stats.leads.negotiating++;
          if (/won/.test(stage)) {
            stats.leads.won++;
            if (valueCol !== -1) stats.pipeline.won_revenue += Number(row[valueCol]) || 0;
          }
        }
      }

      if (priorityCol !== -1) {
        stats.leads.hot = data.filter(r => /hot/i.test(String(r[priorityCol] || ''))).length;
      } else if (stageCol !== -1) {
        stats.leads.hot = data.filter(r => /hot/i.test(String(r[stageCol] || ''))).length;
      }
    }

    // Manual overrides from STATS tab
    const statsTab = ss.getSheetByName('STATS');
    if (statsTab && statsTab.getLastRow() > 0) {
      const rows = statsTab.getRange(1, 1, statsTab.getLastRow(), 2).getValues();
      for (const [key, val] of rows) {
        if (!key || val === '' || val === null) continue;
        const path = String(key).split('.');
        let obj = stats;
        for (let i = 0; i < path.length - 1; i++) {
          if (!obj[path[i]] || typeof obj[path[i]] !== 'object') obj[path[i]] = {};
          obj = obj[path[i]];
        }
        obj[path[path.length - 1]] = val;
      }
    }
  } catch (err) {
    stats._error = String(err);
  }

  return ContentService
    .createTextOutput(JSON.stringify(stats))
    .setMimeType(ContentService.MimeType.JSON);
}

function testRun() {
  const out = doGet();
  Logger.log(out.getContent());
}
```

---

## Verified working timestamps

- **2026-04-27 ~15:38 UTC** — Apps Script endpoint deployed, returns valid JSON, sheet integration confirmed
- **2026-04-27 (earlier)** — 19 `data-zyn` tags added to dashboard.html, all confirmed bound and live-updating
- **2026-04-27 (earlier)** — Auto-update + version-check + cache-busting verified end-to-end (changing `leads.hot` from 0 to 7 in stats.json propagated to dashboard within 30s without reload)

---

## Deferred / Future work

- **Finish Cloudflare lockdown:** zone activation pending. Once active, complete Phases 4–6 of the lockdown plan (Cloudflare Pages deployment, custom domain bind, Cloudflare Access policy)
- **Wire all 19 agents to push to Sheet/Stats** — currently most agents already write to the CRM tab; need to verify each one and add explicit `zynPushStats({...})` calls for trading P&L, outreach counts, contract drafts, etc.
- **Optional: events feed** — scrolling list of "Sara just sent 100 emails" / "Caleb closed +$1247" — separate `events.json` array, ~30 min of work
- **Optional: re-enable DNSSEC on Cloudflare side** once zone is active. Cloudflare → DNS → Settings → DNSSEC → Enable, then add new DS records back at GoDaddy.
- **Cleanup:** delete stale `stats.json` once Cloudflare Pages migration complete; delete dead M365 DNS records (`autodiscover`, `lyncdiscover`, `msoid`, `sip`, M365 verification TXT) from Cloudflare DNS
