"""
setup_telegram.py
One-time setup script for Telegram alerts.
Run this once to configure your bot and get your chat ID.

Steps:
1. python setup_telegram.py
2. Follow the instructions printed
3. Add secrets to GitHub Actions
"""

import requests
import os
from pathlib import Path


def setup_telegram():
    print("""
╔══════════════════════════════════════════════════════╗
║          TELEGRAM ALERT SETUP — ARB QUANT           ║
╚══════════════════════════════════════════════════════╝

STEP 1 — Create your Telegram bot (takes 2 minutes):
  1. Open Telegram app on your phone
  2. Search for @BotFather
  3. Send: /newbot
  4. Name it: Arb Quant Monitor
  5. Username: arb_quant_yourname_bot
  6. BotFather will give you a TOKEN like:
     7123456789:AAHdqTcvCH1vGBJ29_lGDDLiVYnxjNOJaXc

STEP 2 — Get your Chat ID:
  1. Send any message to your new bot
  2. Open this URL in your browser (replace YOUR_TOKEN):
     https://api.telegram.org/botYOUR_TOKEN/getUpdates
  3. Look for "chat":{"id": XXXXXXXXX}
  4. That number is your CHAT_ID

STEP 3 — Test locally:
  Run: python setup_telegram.py --test YOUR_TOKEN YOUR_CHAT_ID

STEP 4 — Add to GitHub Actions secrets:
  1. Go to: github.com/edizere14-create/arb-quant/settings/secrets/actions
  2. Click "New repository secret"
  3. Add: TELEGRAM_BOT_TOKEN = your token
  4. Add: TELEGRAM_CHAT_ID = your chat id

STEP 5 — Add to local .env file:
  The .env file is already in .gitignore so it won't be pushed.
""")

    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        token   = sys.argv[2] if len(sys.argv) > 2 else ""
        chat_id = sys.argv[3] if len(sys.argv) > 3 else ""
    elif len(sys.argv) == 4:
        token   = sys.argv[2]
        chat_id = sys.argv[3]
    else:
        token   = input("Enter your BOT_TOKEN (or press Enter to skip test): ").strip()
        chat_id = input("Enter your CHAT_ID (or press Enter to skip test): ").strip()

    if not token or not chat_id:
        print("Skipping test. Follow steps above to configure.")
        return

    # Test the connection
    print(f"\nTesting connection...")
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r   = requests.post(url, json={
            "chat_id":   chat_id,
            "text":      "✅ *ARB Quant Monitor connected!*\n\nYou will receive alerts when:\n• ⚡ Entry signal fires\n• 🔴 Exit signal fires\n• ⚠️ System errors occur",
            "parse_mode":"Markdown"
        }, timeout=10)

        if r.status_code == 200:
            print("✅ Test message sent! Check your Telegram.")
            print("\nNow save these to GitHub Secrets and .env file:")
            print(f"  TELEGRAM_BOT_TOKEN={token}")
            print(f"  TELEGRAM_CHAT_ID={chat_id}")

            # Write .env file
            env_path = Path(".env")
            env_content = f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n"

            if env_path.exists():
                existing = env_path.read_text()
                # Remove old telegram lines if present
                lines = [l for l in existing.splitlines()
                         if not l.startswith("TELEGRAM_")]
                lines.append(f"TELEGRAM_BOT_TOKEN={token}")
                lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
                env_path.write_text("\n".join(lines) + "\n")
            else:
                env_path.write_text(env_content)

            print(f"\n✅ Saved to .env file (not tracked by git)")
            print(f"\nNext: Add secrets to GitHub Actions:")
            print(f"  github.com/edizere14-create/arb-quant/settings/secrets/actions")
        else:
            print(f"❌ Failed: {r.status_code} {r.text}")

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    setup_telegram()
