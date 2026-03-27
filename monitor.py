#!/usr/bin/env python3
"""
Monitor WordPress → WhatsApp | La Red 106.1
Modo: ejecución única (sin loop). Se lanza vía cron.
"""

import requests
import json
import re
import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from html.parser import HTMLParser

# ── Config ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

GREEN_API_INSTANCE = os.getenv("GREEN_API_INSTANCE_ID")
GREEN_API_TOKEN    = os.getenv("GREEN_API_TOKEN")
GROUP_ID           = os.getenv("WHATSAPP_GROUP_ID")
WP_API_URL         = "https://cms.lared1061.com/wp-json/wp/v2/posts"
SEEN_FILE          = BASE_DIR / "seen_posts.json"
LOG_FILE           = BASE_DIR / "monitor.log"

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, mode='a')]
)
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────
class HTMLStripper(HTMLParser):
    def __init__(self): super().__init__(); self.result = []
    def handle_data(self, d): self.result.append(d)
    def get_text(self): return ' '.join(self.result).strip()

def strip_html(html):
    s = HTMLStripper(); s.feed(html)
    return re.sub(r'\s+', ' ', s.get_text()).strip()

def load_seen():
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def send_whatsapp(message: str) -> bool:
    url = f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
    try:
        r = requests.post(url, json={"chatId": GROUP_ID, "message": message}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Error WhatsApp: {e}")
        return False

def extract_resumen(content_html: str) -> str:
    p = re.search(r'<p[^>]*>.*?[Ll]o que necesitas saber[:\s]*</strong>(.*?)</p>', content_html, re.DOTALL | re.IGNORECASE)
    if p:
        return re.sub(r'^[\s:]+', '', strip_html(p.group(1))).strip()
    p2 = re.search(r'Lo que necesitas saber[:\s]+(.*?)</p>', content_html, re.DOTALL | re.IGNORECASE)
    if p2:
        return strip_html(p2.group(1)).strip()
    return ""

def get_category(post: dict) -> str:
    for group in post.get("_embedded", {}).get("wp:term", []):
        for term in group:
            if term.get("taxonomy") == "category":
                name = term.get("name", "")
                if name.lower() != "sin categoría" and name.lower() != "sin categoria":
                    return name.upper()
    return ""

def get_link(post: dict) -> str:
    return f"https://www.lared1061.com/posts/{post.get('slug', '')}"

def format_message(title: str, category: str, resumen: str, link: str) -> str:
    lines = []
    if category:
        lines.append(f"📰 *{category}*")
    lines.append(f"*{title}*")
    if resumen:
        lines.append(f"\n{resumen}")
    lines.append(f"\n{link}")
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────
def main():
    log.info("--- Revisando WordPress ---")
    seen = load_seen()
    new_count = 0

    try:
        r = requests.get(
            WP_API_URL,
            params={"per_page": 20, "status": "publish", "orderby": "date", "order": "desc", "_embed": "wp:term"},
            timeout=15
        )
        r.raise_for_status()
        posts = r.json()
    except Exception as e:
        log.error(f"Error obteniendo posts: {e}")
        return

    log.info(f"  {len(posts)} posts recibidos")

    for post in reversed(posts):
        post_id = str(post.get("id", ""))
        if not post_id or post_id in seen:
            continue

        title    = strip_html(post.get("title", {}).get("rendered", "Sin título"))
        content  = post.get("content", {}).get("rendered", "")
        link     = get_link(post)
        category = get_category(post)
        resumen  = extract_resumen(content)

        log.info(f"  Nueva nota [{post_id}]: {title[:60]}")
        log.info(f"  Categoría: {category or '(sin categoría)'}")

        msg = format_message(title, category, resumen, link)
        if send_whatsapp(msg):
            seen.add(post_id)
            save_seen(seen)
            new_count += 1
            log.info(f"  ✓ Enviado")
        else:
            log.error(f"  ✗ Fallo al enviar")

    log.info(f"  {new_count} nota(s) enviadas." if new_count else "  Sin notas nuevas.")

if __name__ == "__main__":
    main()
