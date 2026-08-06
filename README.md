# Monitor La Red 106.1 — RSS a WhatsApp

Envía cada nota nueva de lared1061.com al grupo de WhatsApp **Al Aire LRN**.

## Dónde corre

**Railway, servicio always-on 24/7** (`scheduler.py`). Antes corría en GitHub
Actions; se migró el 6 de agosto de 2026 después de que un outage mayor de
Actions dejara el monitor caído varias horas (los jobs nunca eran tomados por un
runner: *"The job was not acquired by Runner of type hosted"*).

## Horario (America/Guatemala)

| Ventana | Frecuencia |
|---|---|
| 05:00 – 14:59 GT | cada 5 min |
| 15:00 – 04:59 GT | cada 20 min |
| Vigilante de latido | cada 15 min |

## Archivos

- `monitor.py` — lectura del RSS, guard anti-duplicado y envío por Green API.
- `scheduler.py` — APScheduler, vigilante de latido y servidor `/health`.
- `seen_posts.json` — **semilla histórica** del guard. Solo se usa la primera vez
  que arranca un volumen vacío. No es la fuente de verdad.
- `.github/workflows/monitor.yml` — respaldo manual de emergencia.

## Fuente del feed

`https://www.lared1061.com/feed` (Vercel). No se usa `cms.lared1061.com` porque
el firewall Sucuri bloquea las IPs de los datacenters. Ojo: `/feed/` responde
308 hacia `/feed`, por eso las peticiones siguen redirects.

El feed **ya no emite `<guid>`**, así que la clave del guard es el `link`
normalizado (sin query, sin barra final). Si el feed vuelve a traer guid
numérico, se usa ese para no reenviar el histórico.

## Guard anti-duplicado

`/data/seen_posts.json` en el volumen de Railway, formato v2:

```json
{"version": 2, "vistos": {"<clave>": "<fecha ISO en que se vio>"}}
```

**Por qué está fechado:** el monitor viejo guardaba `json.dump(list(set))` y un
`set` de Python no conserva orden, así que el orden del archivo heredado es
arbitrario. Cualquier poda del tipo "conservar los últimos N" borra claves
recientes al azar y reenvía notas ya publicadas al grupo (se reprodujo en
pruebas: 8 notas viejas reenviadas). Con fecha, la poda es determinista:
se descarta lo visto hace más de `RETENCION_DIAS` (60 por defecto).

Otras protecciones:
- Escritura atómica (`.tmp` + `replace`): un reinicio nunca deja el guard a medias.
- Si el guard existe pero está ilegible, el proceso **falla en vez de continuar**
  (continuar significaría reenviar el feed completo al grupo).
- La clave solo se marca como vista **después** de un envío exitoso.

## Alertas al administrador

Van **solo** al número `ALERTA_ADMIN` (50240112911), nunca al grupo:

- **Sin latido**: el monitor no corre en `LATIDO_MAX_MIN` (30) min. Reintento de
  aviso cada `REALERTA_HORAS` (2) y mensaje de restablecimiento al volver.
- **Feed vacío**: el RSS responde 200 pero sin notas en 3 revisiones seguidas.

## Variables de entorno

| Variable | Descripción |
|---|---|
| `GREEN_API_INSTANCE_ID` | Instancia Green API (7107565422) |
| `GREEN_API_TOKEN` | Token de la instancia |
| `GREEN_API_BASE` | `https://7107.api.greenapi.com` |
| `WHATSAPP_GROUP_ID` | Grupo Al Aire LRN: `120363183252628978@g.us` |
| `ALERTA_ADMIN` | Número para alertas operativas (`50240112911`) |
| `RSS_URL` | `https://www.lared1061.com/feed` |
| `DATA_DIR` | `/data` (volumen de Railway) |
| `RETENCION_DIAS` | Días que se conserva una clave en el guard (60) |
| `LATIDO_MAX_MIN` | Minutos sin correr antes de alertar (30) |
| `MAX_EDAD_HORAS` | Si es > 0, descarta notas más viejas que eso (0 = sin filtro) |

## Verificar

```
curl https://<dominio>/health
```

Devuelve la hora de la última revisión en GT y los minutos transcurridos.

## Respaldo manual (GitHub Actions)

El workflow quedó **solo como botón de emergencia** y exige el input
`force = si`. Motivo: existe un disparador externo (no ubicado, no está en
Make.com ni en Railway ni en ningún repo) que sigue haciendo `workflow_dispatch`
cada 5 minutos con el PAT de Guillermo. Sin el input, ese disparo termina en un
job que no hace nada, así no duplica mensajes ni pelea con el guard de Railway.

Al usar el respaldo, el guard es el `seen_posts.json` del repo (`SEEN_BACKEND=github`),
que ya está desincronizado del de Railway: **un envío manual puede duplicar notas**.
Usar solo si Railway está caído.
