# ZYN Empire Dashboard — Setup & Operations Guide

Last updated: April 28, 2026

Owner: Zion Marshall coachmarshall72@yahoo.com)

Repo: ancient1zion/zynsl-website](https://github.com/ancient1zion/zynsl-website)

Status: Production — fully migrated, locked down, verified end-to-end

---

## URLs

| URL | Purpose | Auth |

|---|---|---|

| *https://empire.zynsupplyandlogistics.com** | Production dashboard (use this) | Cloudflare Access (only Zion) |

| https://zyn-empire-dashboard.pages.dev | Cloudflare Pages auto-domain | Cloudflare Access (same policy) |

| Apps Script /exec URL | Data endpoint | Public (returns aggregated stats only) |

| https://ancient1zion.github.io/zynsl-website/dashboard.html | Old GitHub Pages URL — deprecated | Public (kept as fallback) |

Bookmark https://empire.zynsupplyandlogistics.com — bare URL, the _redirects file routes it straight to the dashboard.

---

## What this dashboard does

Live operations view for the ZYN Empire — 19 autonomous agents pushing real-time numbers (pipeline, leads, agent status, trading P&L, contracts, outreach) to a single page that auto-updates without manual refresh. Locked down to one email address via Cloudflare Access; only Zion gets in.

---

## Architecture

Agents write to a Google Sheet. An Apps Script Web App reads the sheet on demand and returns aggregated JSON. The dashboard (hosted on Cloudflare Pages, auto-deployed from GitHub) polls the Apps Script every 30 seconds and updates 19 stat elements in place. Cloudflare Access sits in front of the dashboard's custom domain, requiring email-based one-time-PIN authentication for the only allowed email (Zion's).
