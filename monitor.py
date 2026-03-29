#!/usr/bin/env python3
"""
Monitor WordPress → WhatsApp | La Red 106.1
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
WP_API_URL         = "https://cms.lared1061.com/wp-json/wp/v2/posts"
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
    # Guardar local siempre
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

    if not GH_TOKEN:
        return

    content = base64.b64encode(json.dumps(list(seen)).encode()).decode()

    # Si no tenemos sha, obtenerlo
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
                if name.lower() not in ["sin categoría", "sin categoria", "uncategorized"]:
                    return name.upper()
    return ""

def get_link(post: dict) -> str:
    return f"https://www.lared1061.com/posts/{post.get('slug', '')}"

def format_message(title, category, resumen, link):
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
    seen, sha = load_seen()
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
