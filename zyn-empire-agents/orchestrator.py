"""Noah supervisor — main loop. Run forever via PM2."""
import os, json, time
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv
load_dotenv()
from zyn_agent import ZynAgent
from tools import sheets_read, discord_notify, sheets_append

# Configure structured logging
os.makedirs("logs", exist_ok=True)
logger.remove()
logger.add("logs/zyn_empire.log", rotation="10 days", retention="30 days", compression="zip", serialize=True, level="INFO")
logger.add(lambda m: print(m, end=""), level="INFO")

def load_agents():
    with open("agents_config.json") as f:
        cfg = json.load(f)
    return {a["id"]: ZynAgent(a) for a in cfg["agents"]}

def is_stopped() -> bool:
    try:
        v = sheets_read("CONTROL", "A1")
        if not v or not v[0]: return False
        return str(v[0][0]).strip().upper() == "STOP"
    except Exception as e:
        logger.warning(f"STOP check failed (CONTROL tab may not exist yet): {e}")
        return False

def heartbeat():
    discord_notify("noah", f"💓 Empire heartbeat {datetime.utcnow().isoformat()}Z")
    try:
        sheets_append("CONTROL", [datetime.utcnow().isoformat(), "heartbeat", "noah"])
    except Exception:
        pass

def supervisor_tick(agents):
    """One loop iteration: Noah inspects state and dispatches."""
    if is_stopped():
        logger.warning("STOP signal active — skipping tick")
        return
    noah = agents["noah"]
    # Build the observation from current empire state
    try:
        leads = sheets_read("LEADS")
        crm = sheets_read("CRM")
        obs = f"LEADS rows: {len(leads)-1 if leads else 0}\nCRM rows: {len(crm)-1 if crm else 0}\nUTC: {datetime.utcnow().isoformat()}"
    except Exception as e:
        obs = f"State read failed: {e}"
    noah.run(f"Inspect empire state and decide which agent to dispatch.\n{obs}")

def main():
    logger.info("ZYN Empire orchestrator starting")
    agents = load_agents()
    discord_notify("noah", f"🚀 ZYN Empire online · {len(agents)} agents loaded · {datetime.utcnow().isoformat()}Z")
    last_heartbeat = 0
    while True:
        try:
            now = time.time()
            if now - last_heartbeat > 3600:
                heartbeat()
                last_heartbeat = now
            supervisor_tick(agents)
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            discord_notify("noah", "🛑 ZYN Empire shutdown")
            break
        except Exception as e:
            logger.exception(f"Tick failed: {e}")
            discord_notify("noah", f"⚠️ Tick failed: {e}")
        time.sleep(300)  # 5 minute cadence

if __name__ == "__main__":
    main()
