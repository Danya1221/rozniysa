"""Use exec so Railway SIGTERM reaches the bot and closes its session."""
import os
import sys

if __name__ == "__main__":
    setup = os.getenv("SETUP_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    script = "session_web.py" if setup else "retail_main.py"
    print(f"▶️ Запуск: {script}", flush=True)
    os.execv(sys.executable, [sys.executable, "-u", script])
