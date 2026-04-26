// PM2 config — keeps orchestrator alive 24/7
module.exports = {
  apps: [{
    name: "zyn-orchestrator",
    script: "orchestrator.py",
    interpreter: "python3",
    cwd: __dirname,
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: "1G",
    restart_delay: 5000,
    max_restarts: 10,
    min_uptime: "60s",
    error_file: "logs/pm2-error.log",
    out_file: "logs/pm2-out.log",
    merge_logs: true,
    time: true,
    env: { PYTHONUNBUFFERED: "1" }
  }]
};
