#!/usr/bin/env python3
"""
Monitor RSS -> WhatsApp | La Red 106.1

Fuente: RSS publico (www.lared1061.com/feed) servido desde Vercel, NO pasa por
el firewall Sucuri de cms.lared1061.com.

Guard anti-duplicado: /data/seen_posts.json (volumen persistente de Railway).
Se siembra una sola vez desde el seen_posts.json del repo en el primer arranque.

Este modulo expone revisar_rss() para que lo llame el scheduler. Tambien puede
correrse suelto (python3 monitor.py) para una revision manual.
"""

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

TZ_GT = ZoneInfo("America/Guatemala")

# -- Config ------------------------------------------------
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

GREEN_API_INSTANCE = os.getenv("GREEN_API_INSTANCE_ID")
GREEN_API_TOKEN = os.getenv("GREEN_API_TOKEN")
GREEN_API_BASE = os.getenv("GREEN_API_BASE", "https://7107.api.greenapi.com")
GROUP_ID = os.getenv("WHATSAPP_GROUP_ID")
ALERTA_ADMIN = os.getenv("ALERTA_ADMIN", "50240112911")
RSS_URL = os.getenv("RSS_URL", "https://www.lared1061.com/feed")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SEEN_FILE = DATA_DIR / "seen_posts.json"
SEED_FILE = BASE_DIR / "seen_posts.json"
LATIDO_FILE = DATA_DIR / "latido.txt"
ESTADO_FILE = DATA_DIR / "estado.json"

MAX_EDAD_HORAS = int(os.getenv("MAX_EDAD_HORAS", "0"))  # 0 = sin filtro de edad

# Compatibilidad con el modo GitHub Actions (respaldo manual)
GH_TOKEN = os.getenv("GH_TOKEN")
GH_REPO = os.getenv("GH_REPO", "gvelarde-rgb/lared-monitor")
USAR_GITHUB = os.getenv("SEEN_BACKEND", "local").lower() == "github"

# -- Logging -----------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("monitor")


# -- Helpers -----------------------------------------------
class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []

    def handle_data(self, d):
        self.result.append(d)

    def get_text(self):
        return " ".join(self.result).strip()


def strip_html(html):
    s = HTMLStripper()
    s.feed(html)
    return re.sub(r"\s+", " ", s.get_text()).strip()


def clave_canonica(item):
    """Clave estable del guard.

    El feed de Vercel dejo de emitir <guid>, asi que el link normalizado es la
    referencia mas confiable. Si el feed vuelve a traer guid numerico, se usa ese
    para no reenviar el historico que ya vive en el guard.
    """
    guid = (item.get("guid") or "").strip()
    if guid and not guid.startswith("http"):
        return guid
    link = (item.get("link") or "").strip()
    link = link.split("?")[0].split("#")[0].rstrip("/")
    return link


# -- Guard -------------------------------------------------
# El guard local es un dict {clave: fecha_iso} (formato v2).
#
# Por que fechado y no una lista: el monitor viejo guardaba json.dump(list(set))
# y un set de Python NO conserva orden, asi que el orden del archivo heredado es
# arbitrario. Podar "los ultimos N" de esa lista borra claves recientes al azar y
# reenvia notas ya publicadas al grupo (bug verificado en pruebas). Con fecha la
# poda es determinista: se conserva lo visto en los ultimos RETENCION_DIAS.
RETENCION_DIAS = int(os.getenv("RETENCION_DIAS", "60"))


def _leer_github():
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GH_REPO}/contents/seen_posts.json",
            headers={"Authorization": f"token {GH_TOKEN}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = json.loads(base64.b64decode(r.json()["content"]))
            log.info("  seen_posts cargado desde GitHub: %s claves", len(data))
            return list(data), r.json()["sha"]
    except Exception as e:
        log.warning("  No se pudo cargar desde GitHub: %s", e)
    return None, None


def _normalizar(data):
    """Acepta el formato viejo (lista) o el v2 (dict) y devuelve dict fechado."""
    ahora = datetime.now(timezone.utc).isoformat()
    if isinstance(data, dict):
        if "vistos" in data:
            return dict(data["vistos"])
        return dict(data)
    # Lista heredada: sin fechas reales. Se les pone la fecha de migracion para
    # que sobrevivan la ventana de retencion completa y luego caigan solas.
    return {k: ahora for k in data}


def load_seen():
    """Devuelve (guard_dict, sha). guard_dict = {clave: fecha_iso}."""
    if USAR_GITHUB and GH_TOKEN:
        data, sha = _leer_github()
        if data is not None:
            return _normalizar(data), sha

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE) as f:
                data = json.load(f)
            guard = _normalizar(data)
            log.info("  guard local: %s claves (%s)", len(guard), SEEN_FILE)
            return guard, None
        except Exception as e:
            # Guard ilegible: NO seguir, se reenviaria todo el feed al grupo.
            log.error("  guard local corrupto (%s): %s", SEEN_FILE, e)
            raise

    # Primer arranque en el volumen: sembrar desde el archivo del repo
    if SEED_FILE.exists():
        with open(SEED_FILE) as f:
            data = json.load(f)
        guard = _normalizar(data)
        log.info("  SIEMBRA inicial del guard desde el repo: %s claves", len(guard))
        _escribir_local(guard)
        return guard, None

    log.warning("  guard vacio: no hay %s ni semilla en el repo", SEEN_FILE)
    return {}, None


def _podar(guard):
    """Descarta claves vistas hace mas de RETENCION_DIAS."""
    limite = datetime.now(timezone.utc) - timedelta(days=RETENCION_DIAS)
    fuera = []
    for k, v in guard.items():
        try:
            if datetime.fromisoformat(v) < limite:
                fuera.append(k)
        except Exception:
            continue  # fecha ilegible: se conserva, es mas seguro
    for k in fuera:
        del guard[k]
    if fuera:
        log.info("  poda: %s claves con mas de %s dias", len(fuera), RETENCION_DIAS)
    return guard


def _escribir_local(guard):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump({"version": 2, "vistos": guard}, f)
    tmp.replace(SEEN_FILE)  # atomico: nunca deja un guard a medio escribir


def save_seen(guard, sha=None):
    guard = _podar(guard)

    if USAR_GITHUB and GH_TOKEN:
        # El respaldo de GitHub Actions conserva el formato lista, sin poda.
        content = base64.b64encode(json.dumps(list(guard.keys())).encode()).decode()
        if not sha:
            r = requests.get(
                f"https://api.github.com/repos/{GH_REPO}/contents/seen_posts.json",
                headers={"Authorization": f"token {GH_TOKEN}"},
                timeout=10,
            )
            if r.status_code == 200:
                sha = r.json()["sha"]
        r = requests.put(
            f"https://api.github.com/repos/{GH_REPO}/contents/seen_posts.json",
            headers={"Authorization": f"token {GH_TOKEN}"},
            json={
                "message": "chore: update seen posts [skip ci]",
                "content": content,
                "sha": sha,
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            log.info("  guard guardado en GitHub")
        else:
            log.warning("  Error guardando en GitHub: %s %s", r.status_code, r.text[:120])
        return

    _escribir_local(guard)
    log.info("  guard guardado: %s claves", len(guard))


# -- Estado / latido ---------------------------------------
def leer_estado():
    if ESTADO_FILE.exists():
        try:
            with open(ESTADO_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def guardar_estado(estado):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(estado, f)
    tmp.replace(ESTADO_FILE)


def escribir_latido():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LATIDO_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def leer_latido():
    if not LATIDO_FILE.exists():
        return None
    try:
        return datetime.fromisoformat(LATIDO_FILE.read_text().strip())
    except Exception:
        return None


# -- WhatsApp ----------------------------------------------
def send_whatsapp(message: str, chat_id: str = None) -> bool:
    destino = chat_id or GROUP_ID
    url = (
        f"{GREEN_API_BASE}/waInstance{GREEN_API_INSTANCE}"
        f"/sendMessage/{GREEN_API_TOKEN}"
    )
    try:
        r = requests.post(
            url, json={"chatId": destino, "message": message}, timeout=20
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Error WhatsApp (%s): %s", destino, e)
        return False


def avisar_admin(texto: str) -> bool:
    """Aviso operativo, SOLO al numero del administrador."""
    return send_whatsapp(texto, f"{ALERTA_ADMIN}@c.us")


def format_message(title, category, resumen, link):
    lines = []
    if category:
        lines.append(f"📰 *{category.upper()}*")
    lines.append(f"*{title}*")
    if resumen:
        lines.append(f"\n{resumen}")
    lines.append(f"\n{link}")
    return "\n".join(lines)


# -- RSS ---------------------------------------------------
def fetch_rss_items():
    try:
        r = requests.get(
            RSS_URL,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,  # /feed/ responde 308 -> /feed
        )
        r.raise_for_status()
        xml = r.text
    except Exception as e:
        log.error("Error obteniendo RSS: %s", e)
        return None  # None = fallo de red (distinto de feed vacio)

    items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    parsed = []
    for it in items:

        def tag(name):
            m = re.search(
                rf"<{name}>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{name}>",
                it,
                re.DOTALL,
            )
            return m.group(1).strip() if m else ""

        title = strip_html(tag("title"))
        link = tag("link").strip()
        guid = tag("guid").strip()
        category = strip_html(tag("category"))
        desc = strip_html(tag("description"))
        pub = tag("pubDate").strip()

        if desc.lower() == title.lower():
            desc = ""
        if len(desc) > 320:
            desc = desc[:317] + "..."

        parsed.append(
            {
                "guid": guid,
                "title": title,
                "link": link,
                "category": category,
                "resumen": desc,
                "pubDate": pub,
            }
        )
    return parsed


def _pub_dt(pub):
    """
    Convierte el pubDate del feed a datetime UTC real.

    OJO: el feed de Next.js emite la hora LOCAL de Guatemala pero la etiqueta
    como GMT. Ejemplo real: una nota publicada 17:48 GT sale como
    "Fri, 07 Aug 2026 17:48:20 GMT". Si se toma literal, toda nota nace con
    ~6 h de edad falsa y el filtro de antiguedad la descarta.

    Regla: si el offset viene en cero (GMT / +0000), el reloj se reinterpreta
    como hora de Guatemala. Si algun dia el feed emite un offset real distinto
    de cero, se respeta tal cual.
    """
    if not pub:
        return None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(pub)
    except Exception:
        return None

    if dt.tzinfo is None or dt.utcoffset() == timedelta(0):
        # Reloj local de Guatemala mal etiquetado como GMT
        dt = dt.replace(tzinfo=TZ_GT)

    return dt.astimezone(timezone.utc)


def _edad_horas(pub):
    dt = _pub_dt(pub)
    if dt is None:
        return None

    edad = (datetime.now(timezone.utc) - dt).total_seconds() / 3600

    if edad < 0:
        # Nota "del futuro": desfase de zona horaria en el feed.
        # Se trata como recien publicada para no descartarla nunca por fecha.
        log.warning(
            "  pubDate en el futuro (%.1f h): '%s'. Se asume recien publicada.",
            edad,
            pub,
        )
        return 0.0

    return edad


# -- Revision principal ------------------------------------
def revisar_rss():
    """Una pasada del monitor. Devuelve dict con el resultado."""
    log.info("--- Revisando RSS ---")
    guard, sha = load_seen()
    estado = leer_estado()
    enviadas = 0
    cambios = 0
    omitidas_viejas = 0

    items = fetch_rss_items()

    if items is None:
        estado["fallos_feed"] = estado.get("fallos_feed", 0) + 1
        guardar_estado(estado)
        log.warning("  Fallo de red al leer el RSS (%s consecutivos)", estado["fallos_feed"])
        return {"ok": False, "motivo": "red", "enviadas": 0}

    if not items:
        estado["feed_vacio"] = estado.get("feed_vacio", 0) + 1
        guardar_estado(estado)
        log.warning("  RSS respondio pero sin items (%s consecutivos)", estado["feed_vacio"])
        if estado["feed_vacio"] == 3:
            avisar_admin(
                "MONITOR LA RED\n"
                "El RSS responde pero viene sin notas en 3 revisiones seguidas.\n"
                "Revisar el feed de Vercel: " + RSS_URL
            )
        return {"ok": True, "motivo": "feed vacio", "enviadas": 0}

    # El feed respondio con contenido: se limpian los contadores de falla
    if estado.get("feed_vacio") or estado.get("fallos_feed"):
        estado["feed_vacio"] = 0
        estado["fallos_feed"] = 0

    log.info("  %s items recibidos", len(items))

    # RSS viene del mas nuevo al mas viejo: procesar en orden cronologico
    for item in reversed(items):
        clave = clave_canonica(item)
        if not clave or clave in guard:
            continue

        edad = _edad_horas(item["pubDate"])

        if MAX_EDAD_HORAS and edad is not None and edad > MAX_EDAD_HORAS:
            # Solo se marca como vista si es CLARAMENTE vieja. En la zona gris
            # se deja sin marcar para que un error de fecha no queme la nota
            # para siempre: el siguiente ciclo la vuelve a considerar.
            if edad > MAX_EDAD_HORAS * 1.5:
                log.info(
                    "  Omitida por antiguedad (%.1f h, marcada vista): %s",
                    edad,
                    item["title"][:60],
                )
                guard[clave] = datetime.now(timezone.utc).isoformat()
                cambios += 1
            else:
                log.warning(
                    "  Omitida por antiguedad (%.1f h, zona gris, NO marcada): %s",
                    edad,
                    item["title"][:60],
                )
            omitidas_viejas += 1
            continue

        log.info(
            "  Nueva nota (edad %s): %s",
            "?" if edad is None else "%.2f h" % edad,
            item["title"][:70],
        )
        log.info("  Categoria: %s", item["category"] or "(sin categoria)")

        msg = format_message(
            item["title"], item["category"], item["resumen"], item["link"]
        )
        if send_whatsapp(msg):
            guard[clave] = datetime.now(timezone.utc).isoformat()
            enviadas += 1
            cambios += 1
            log.info("  OK enviado")
        else:
            log.error("  FALLO al enviar, se reintenta en la proxima revision")

    if cambios:
        save_seen(guard, sha)
        log.info("  %s nota(s) enviadas.", enviadas)
    else:
        log.info("  Sin notas nuevas.")

    # Rastro para el vigilante de flujo: no basta con que el monitor corra,
    # tiene que estar SALIENDO algo.
    ahora_iso = datetime.now(timezone.utc).isoformat()
    if enviadas:
        estado["ultimo_envio_ok"] = ahora_iso
        estado["omitidas_viejas_seguidas"] = 0
    elif omitidas_viejas:
        estado["omitidas_viejas_seguidas"] = (
            estado.get("omitidas_viejas_seguidas", 0) + omitidas_viejas
        )
    estado["ultima_revision"] = ahora_iso
    estado["ultimo_items"] = len(items)
    estado["ultimas_omitidas_viejas"] = omitidas_viejas

    guardar_estado(estado)
    escribir_latido()
    return {
        "ok": True,
        "enviadas": enviadas,
        "items": len(items),
        "omitidas_viejas": omitidas_viejas,
    }


if __name__ == "__main__":
    revisar_rss()
