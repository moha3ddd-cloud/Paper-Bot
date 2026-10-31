"""Telegram diagnostic: never prints the token or the chat id, only what is wrong."""
import os
import requests

tok = os.getenv("TELEGRAM_TOKEN", "")
chat = os.getenv("TELEGRAM_CHAT_ID", "")
out = [
    f"token: set={bool(tok)} len={len(tok)} has_colon={':' in tok} has_space_or_newline={tok != tok.strip()}",
    f"chat id: set={bool(chat)} len={len(chat)} numeric={chat.strip().lstrip('-').isdigit()} "
    f"has_space_or_newline={chat != chat.strip()}",
]


def desc(r):
    try:
        j = r.json()
        return j.get("description") or ("ok" if j.get("ok") else str(j)[:120])
    except Exception:
        return r.text[:120]


if tok:
    base = f"https://api.telegram.org/bot{tok.strip()}"
    try:
        r = requests.get(base + "/getMe", timeout=15)
        extra = ""
        if r.ok:
            extra = " bot=@" + r.json()["result"].get("username", "?")
        out.append(f"getMe: HTTP {r.status_code} {desc(r)}{extra}")
        if r.ok and chat:
            r = requests.post(base + "/sendMessage", timeout=15,
                              json={"chat_id": chat.strip(), "text": "✅ Telegram test from paper-bot"})
            out.append(f"sendMessage: HTTP {r.status_code} {desc(r)}")
        r = requests.get(base + "/getUpdates", timeout=15)
        if r.ok:
            res = r.json().get("result", [])
            ids = {str((u.get("message") or {}).get("chat", {}).get("id")) for u in res if u.get("message")}
            out.append(f"getUpdates: {len(res)} update(s) from {len(ids)} chat(s); "
                       f"configured chat id among them: {chat.strip() in ids}")
        else:
            out.append(f"getUpdates: HTTP {r.status_code} {desc(r)}")
    except Exception as e:
        out.append(f"request failed: {type(e).__name__}")
else:
    out.append("TELEGRAM_TOKEN secret is empty or the name is wrong")

text = "\n".join(out)
print(text)
os.makedirs("data", exist_ok=True)
with open("data/telegram_test.txt", "w") as f:
    f.write(text + "\n")
