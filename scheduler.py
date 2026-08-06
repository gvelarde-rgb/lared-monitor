#!/usr/bin/env python3
"""
Servicio always-on del Monitor La Red 106.1 (Railway).

Corre 3 cosas en el mismo proceso:
  1. Scheduler APScheduler (timezone America/Guatemala)
       - cada 5 min de 05:00 a 14:59 GT  (ventana caliente)
       - cada 20 min el resto del dia
  2. Vigilante de latido: si el monitor no corre en LATIDO_MAX_MIN minutos,
     avisa por WhatsApp SOLO al numero administrador.
  3. Servidor HTTP minimo con /health para verificar sin abrir logs.
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import monitor
from monitor import (
    avisar_admin,
    guardar_estado,
    leer_estado,
    leer_latido,
    revisar_rss,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Guatemala")
PORT = int(os.getenv("PORT", "8080"))
LATIDO_MAX_MIN = int(os.getenv("LATIDO_MAX_MIN", "30"))
REALERTA_HORAS = int(os.getenv("REALERTA_HORAS", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scheduler")

_lock = threading.Lock()


def tarea_monitor():
    """Una revision. El lock evita solapes si una corrida se alarga."""
    if not _lock.acquire(blocking=False):
        log.warning("Revision anterior aun en curso, se omite este ciclo")
        return
    try:
        revisar_rss()
    except Exception as e:
        log.exception("Error no controlado en la revision: %s", e)
    finally:
        _lock.release()


def vigilante_latido():
    """Alerta al admin si el monitor dejo de latir. Anti-spam incluido."""
    ahora = datetime.now(timezone.utc)
    latido = leer_latido()
    estado = leer_estado()

    if latido is None:
        log.info("Vigilante: aun no hay primer latido")
        return

    atraso_min = (ahora - latido).total_seconds() / 60

    if atraso_min <= LATIDO_MAX_MIN:
        # Recuperado: si habiamos alertado, avisar que volvio
        if estado.get("alerta_latido_activa"):
            local = latido.astimezone(TZ).strftime("%H:%M")
            avisar_admin(
                "MONITOR LA RED\n"
                f"Servicio restablecido. Ultima revision {local} GT."
            )
            estado["alerta_latido_activa"] = False
            estado.pop("ultima_alerta_latido", None)
            guardar_estado(estado)
        return

    # Sin latido: alertar respetando la ventana anti-spam
    ultima = estado.get("ultima_alerta_latido")
    if ultima:
        try:
            if ahora - datetime.fromisoformat(ultima) < timedelta(hours=REALERTA_HORAS):
                log.warning(
                    "Sin latido (%.0f min) pero dentro de la ventana anti-spam",
                    atraso_min,
                )
                return
        except Exception:
            pass

    local = latido.astimezone(TZ).strftime("%d/%m %H:%M")
    ok = avisar_admin(
        "MONITOR LA RED SIN LATIDO\n"
        f"Ultima revision: {local} GT ({atraso_min:.0f} min sin correr).\n"
        "El envio de notas al grupo Al Aire LRN puede estar detenido."
    )
    log.error("Sin latido hace %.0f min. Aviso enviado: %s", atraso_min, ok)
    estado["alerta_latido_activa"] = True
    estado["ultima_alerta_latido"] = ahora.isoformat()
    guardar_estado(estado)


class Salud(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") not in ("", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        latido = leer_latido()
        ahora = datetime.now(timezone.utc)
        if latido:
            atraso = round((ahora - latido).total_seconds() / 60, 1)
            cuerpo = {
                "servicio": "lared-monitor",
                "estado": "ok" if atraso <= LATIDO_MAX_MIN else "sin latido",
                "ultima_revision_gt": latido.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "minutos_desde_ultima_revision": atraso,
            }
        else:
            cuerpo = {"servicio": "lared-monitor", "estado": "arrancando"}

        datos = dumps(cuerpo, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, *args):
        pass  # sin ruido de health checks en los logs


def servidor_http():
    ThreadingHTTPServer(("0.0.0.0", PORT), Salud).serve_forever()


def main():
    log.info("Monitor La Red 106.1 - servicio always-on")
    log.info("RSS: %s", monitor.RSS_URL)
    log.info("Grupo destino: %s", monitor.GROUP_ID)
    log.info("Alertas admin: %s", monitor.ALERTA_ADMIN)
    log.info("Guard: %s", monitor.SEEN_FILE)

    threading.Thread(target=servidor_http, daemon=True).start()

    sched = BackgroundScheduler(timezone=TZ)

    # Ventana caliente 05:00-14:59 GT -> cada 5 minutos
    sched.add_job(
        tarea_monitor,
        CronTrigger(hour="5-14", minute="*/5", timezone=TZ),
        id="monitor_caliente",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    # Resto del dia -> cada 20 minutos
    sched.add_job(
        tarea_monitor,
        CronTrigger(hour="0-4,15-23", minute="*/20", timezone=TZ),
        id="monitor_normal",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    # Vigilante de latido cada 15 minutos
    sched.add_job(
        vigilante_latido,
        CronTrigger(minute="*/15", timezone=TZ),
        id="vigilante",
        max_instances=1,
        coalesce=True,
    )

    sched.start()

    # Primera revision inmediata al arrancar
    tarea_monitor()

    log.info("Scheduler activo. Jobs: %s", [j.id for j in sched.get_jobs()])
    try:
        threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


if __name__ == "__main__":
    main()
