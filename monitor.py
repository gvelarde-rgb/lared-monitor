#!/usr/bin/env python3
"""
Monitor RSS → WhatsApp | La Red 106.1
Fuente: RSS público (www.lared1061.com/feed) — servido desde Vercel,
NO pasa por el firewall Sucuri de cms.lared1061.com que bloquea
las IPs de GitHub Actions.
seen_posts.json se actualiza via GitHub API (sin git push, sin conflictos)
"""

import requests
import json
import re
import os
import logging
import base64
from pathlib import Path
from dotenv import load_dotenv
from html.parser import HTMLParser

# ── Config ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

GREEN_API_INSTANCE = os.getenv("GREEN_API_INSTANCE_ID")
GREEN_API_TOKEN    = os.getenv("GREEN_API_TOKEN")
GROUP_ID           = os.getenv("WHATSAPP_GROUP_ID")
GH_TOKEN           = os.getenv("GH_TOKEN")
GH_REPO            = os.getenv("GH_REPO", "gvelarde-rgb/lared-monitor")
RSS_URL            = os.getenv("RSS_URL", "https://www.lared1061.com/feed/")
SEEN_FILE          = BASE_DIR / "seen_posts.json"

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]  # stdout → visible en GitHub Actions
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
    """Carga seen_posts desde GitHub API (siempre fresco, sin conflictos)"""
    if GH_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GH_REPO}/contents/seen_posts.json",
                headers={"Authorization": f"token {GH_TOKEN}"},
                timeout=10
            )
            if r.status_code == 200:
                data = json.loads(base64.b64decode(r.json()["content"]))
                log.info(f"  seen_posts cargado desde GitHub: {len(data)} IDs")
                return set(data), r.json()["sha"]
        except Exception as e:
            log.warning(f"  No se pudo cargar desde GitHub: {e}")
    # Fallback local
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            data = json.load(f)
            return set(data), None
    return set(), None

def save_seen(seen, sha=None):
    """Guarda seen_posts via GitHub API con retry en caso de conflicto"""
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

    if not GH_TOKEN:
        return

    content = base64.b64encode(json.dumps(list(seen)).encode()).decode()

    if not sha:
        r = requests.get(
            f"https://api.github.com/repos/{GH_REPO}/contents/seen_posts.json",
            headers={"Authorization": f"token {GH_TOKEN}"},
            timeout=10
        )
        if r.status_code == 200:
            sha = r.json()["sha"]

    payload = {
        "message": "chore: update seen posts [skip ci]",
        "content": content,
        "sha": sha
    }

    r = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/contents/seen_posts.json",
        headers={"Authorization": f"token {GH_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=15
    )
    if r.status_code in [200, 201]:
        log.info(f"  seen_posts guardado en GitHub")
    else:
        log.warning(f"  Error guardando en GitHub: {r.status_code} {r.text[:100]}")

def send_whatsapp(message: str) -> bool:
    url = f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
    try:
        r = requests.post(url, json={"chatId": GROUP_ID, "message": message}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Error WhatsApp: {e}")
        return False

def format_message(title, category, resumen, link):
    lines = []
    if category:
        lines.append(f"📰 *{category.upper()}*")
    lines.append(f"*{title}*")
    if resumen:
        lines.append(f"\n{resumen}")
    lines.append(f"\n{link}")
    return "\n".join(lines)

# ── RSS parsing ───────────────────────────────────────────
def fetch_rss_items():
    """Descarga y parsea el RSS público de La Red (servido desde Vercel,
    no bloqueado por el firewall Sucuri de cms.lared1061.com)"""
    try:
        r = requests.get(RSS_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        xml = r.text
    except Exception as e:
        log.error(f"Error obteniendo RSS: {e}")
        return []

    items = re.findall(r'<item>(.*?)</item>', xml, re.DOTALL)
    parsed = []
    for it in items:
        def tag(name):
            m = re.search(rf'<{name}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{name}>', it, re.DOTALL)
            return m.group(1).strip() if m else ""

        title    = strip_html(tag("title"))
        link     = tag("link").strip()
        guid     = tag("guid").strip() or link
        category = strip_html(tag("category"))
        desc     = strip_html(tag("description"))

        # Si la descripción es igual al título (no aporta nada), descartarla
        if desc.lower() == title.lower():
            desc = ""

        # Truncar resumen largo
        if len(desc) > 320:
            desc = desc[:317] + "..."

        parsed.append({
            "guid": guid,
            "title": title,
            "link": link,
            "category": category,
            "resumen": desc,
        })
    return parsed

# ── Main ──────────────────────────────────────────────────
def main():
    log.info("--- Revisando RSS ---")
    seen, sha = load_seen()
    new_count = 0

    items = fetch_rss_items()
    if not items:
        log.warning("  No se obtuvieron items del RSS.")
        return

    log.info(f"  {len(items)} items recibidos")

    # Procesar en orden cronológico (RSS viene del más nuevo al más viejo)
    for item in reversed(items):
        guid = item["guid"]
        if not guid or guid in seen:
            continue

        log.info(f"  Nueva nota: {item['title'][:60]}")
        log.info(f"  Categoría: {item['category'] or '(sin categoría)'}")

        msg = format_message(item["title"], item["category"], item["resumen"], item["link"])
        if send_whatsapp(msg):
            seen.add(guid)
            new_count += 1
            log.info(f"  ✓ Enviado")
        else:
            log.error(f"  ✗ Fallo al enviar")

    if new_count > 0:
        save_seen(seen, sha)
        log.info(f"  {new_count} nota(s) enviadas.")
    else:
        log.info("  Sin notas nuevas.")

if __name__ == "__main__":
    main()
