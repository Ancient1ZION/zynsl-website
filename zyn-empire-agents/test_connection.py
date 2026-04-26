"""Pre-flight checks — run before deploying orchestrator."""
import os, sys
from dotenv import load_dotenv
load_dotenv()

def check(name, fn):
    try:
        result = fn()
        print(f"  ✅ {name}: {result}")
        return True
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        return False

def main():
    print("ZYN Empire — connection tests\n")
    ok = 0; total = 0

    total += 1
    if check("env vars present", lambda: f"{sum(1 for k in ['GROQ_API_KEY','GEMINI_API_KEY','SHEET_ID'] if os.getenv(k))}/3 keys set"): ok += 1

    total += 1
    if check("Groq LLM", lambda: __import__('llm_router').router.chat([{"role":"user","content":"reply OK"}])[:30]): ok += 1

    total += 1
    if check("Google Sheets read", lambda: f"{len(__import__('tools').sheets_read('LEADS')[:5])} rows from LEADS"): ok += 1

    total += 1
    if check("Discord proxy", lambda: __import__('tools').discord_notify("noah","🧪 test_connection.py ping")): ok += 1

    total += 1
    if check("DuckDuckGo search", lambda: f"{len(__import__('tools').web_search('zyn empire test',2))} results"): ok += 1

    print(f"\n{ok}/{total} checks passed")
    sys.exit(0 if ok == total else 1)

if __name__ == "__main__":
    main()
