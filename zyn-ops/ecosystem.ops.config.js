// PM2 ecosystem config for ZYN Empire mission-control daemons.
// Sits alongside zyn-empire-agents/ecosystem.config.js (the agent stack).
//
// Usage:
//   pm2 start zyn-ops/ecosystem.ops.config.js
//   pm2 save
//
// Each daemon runs under the project venv so deps are isolated.

const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    {
      name: 'mission_control',
      script: PYTHON,
      args: [path.join(ROOT, 'zyn-ops', 'mission_control.py')],
      cwd: ROOT,
      autorestart: true,
      max_restarts: 20,
      min_uptime: '30s',
      restart_delay: 5000,
      kill_timeout: 8000,
      max_memory_restart: '256M',
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
    {
      name: 'health_audit',
      script: PYTHON,
      args: [path.join(ROOT, 'zyn-ops', 'health_audit.py')],
      cwd: ROOT,
      autorestart: true,
      max_restarts: 20,
      min_uptime: '30s',
      restart_delay: 10000,
      max_memory_restart: '256M',
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
    {
      name: 'drift_detector',
      script: PYTHON,
      args: [path.join(ROOT, 'zyn-ops', 'drift_detector.py')],
      cwd: ROOT,
      autorestart: true,
      max_restarts: 20,
      min_uptime: '30s',
      restart_delay: 10000,
      max_memory_restart: '256M',
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
