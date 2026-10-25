"""Registro del día: qué se ha cerrado y cuándo, y cada re-plano.

Dos salidas complementarias:
- El cierre se marca en el INBOX (fuente de verdad de las tareas): la línea
  pasa a `- [x] ... (hecha: YYYY-MM-DD HH:MM)` — así el parser la ignora en
  los planes futuros Y el histórico queda en el fichero que el usuario ve.
- Cada evento (cierre, re-plan) se añade a out/<fecha>/eventos.jsonl: el
  diario del día que leerán el re-plan y (F2) el chat de contexto.
"""

import fcntl
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import entrada

log = logging.getLogger("registro")

# I8 (2026-10-25): cierra `- [ ] …` y también `* [ ] …` (con el sangrado que
# tenga); el replace de texto fijo de antes dejaba abierta la de asterisco.
SIN_CERRAR = re.compile(r"^(\s*[-*]\s+)\[ \]")


def ahora() -> datetime:
    return datetime.now().astimezone()


def fecha_de_plan(momento: datetime | None = None) -> str:
    """Fecha (YYYY-MM-DD) del 'día de plan' para etiquetar out/<fecha>/.

    B1 (2026-10-25): pipeline usaba UTC y servidor la hora local, así que
    las etiquetas divergían entre las 00:00 y la 01:00 locales. Convención
    única aquí: la fecha LOCAL, restando un día si la hora es < 06:00 —
    tras medianoche sigue siendo el día que empezó a las 07:30 (misma
    regla que _minutos del planificador y la ventana 07:30–01:00).
    """
    momento = momento or ahora()
    fecha = momento.date()
    if momento.hour < 6:
        fecha -= timedelta(days=1)
    return fecha.isoformat()


def cerrar_en_inbox(ruta_inbox: Path, id_tarea: str, momento: datetime | None = None) -> bool:
    """Marca la tarea id como hecha en el fichero del inbox, con timestamp.

    Localiza la línea pendiente parseándola (los ids derivan de
    categoría+texto, así que el parseo la identifica). Conserva el meta de
    la línea: solo cambia el checkbox y añade la marca de hecha. Devuelve
    True si la línea cambió. No toca líneas ajenas.
    """
    momento = momento or ahora()
    # I2 (2026-10-25): el read-modify-write del inbox NO puede correr sin
    # candado — los endpoints síncronos de FastAPI van por un threadpool y
    # dos cierres concurrentes se pisaban (el segundo leía una copia vieja
    # deshaciendo el primero). flock EXCLUSIVO sobre un fichero de lock que
    # vive junto al inbox (data/inbox.lock): cubre también CLI/pipeline en
    # OTRO proceso, cosa que un threading.Lock no puede hacer. Se crea si
    # no existe y NUNCA se borra.
    ruta_lock = ruta_inbox.with_suffix(".lock")
    ruta_lock.parent.mkdir(parents=True, exist_ok=True)
    with ruta_lock.open("a", encoding="utf-8") as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        try:
            return _cerrar_en_inbox_cubierta(ruta_inbox, id_tarea, momento)
        finally:
            fcntl.flock(candado, fcntl.LOCK_UN)


def _cerrar_en_inbox_cubierta(ruta_inbox: Path, id_tarea: str, momento: datetime) -> bool:
    """El cuerpo del cierre, ya bajo el flock del inbox (ver cerrar_en_inbox)."""
    lineas = ruta_inbox.read_text(encoding="utf-8").splitlines()
    for i, linea in enumerate(lineas):
        parseada = entrada.parsear_linea(linea)
        if parseada is not None and parseada.id == id_tarea:
            # El checkbox cambia y la marca va AL FINAL de la línea: el
            # parser quita '(hecha:...)' antes de computar el id, así que
            # la tarea conserva su id estable tras cerrarse.
            con_hecha = SIN_CERRAR.sub(r"\1[x]", linea, count=1)
            if con_hecha == linea:
                # I8: el checkbox no se cerró (formato raro, p. ej. sin
                # marcador de lista): NO se toca la línea, mejor fallar ruidoso.
                log.warning(
                    "No pude cerrar el checkbox de la tarea %s (formato de línea no reconocido): %r",
                    id_tarea, linea,
                )
                return False
            lineas[i] = con_hecha.rstrip() + f" (hecha: {momento.strftime('%Y-%m-%d %H:%M')})"
            ruta_inbox.write_text("\n".join(lineas) + "\n", encoding="utf-8")
            log.info("Tarea %s cerrada en %s (%s)", id_tarea, ruta_inbox, momento.strftime("%H:%M"))
            return True
    log.warning("No encontré en %s ninguna tarea pendiente con id %s", ruta_inbox, id_tarea)
    return False


def registrar_evento(salida_fecha: Path, tipo: str, datos: dict) -> None:
    """Añade una línea al diario del día (eventos.jsonl). Nunca lanza."""
    try:
        salida_fecha.mkdir(parents=True, exist_ok=True)
        evento = {"ts": ahora().isoformat(timespec="seconds"), "tipo": tipo, **datos}
        with (salida_fecha / "eventos.jsonl").open("a", encoding="utf-8") as fichero:
            fichero.write(json.dumps(evento, ensure_ascii=False) + "\n")
    except Exception as error:  # noqa: BLE001 — el diario no puede tumbar el flujo
        log.warning("No pude registrar el evento %s: %s", tipo, error)


def leer_eventos(salida_fecha: Path) -> list[dict]:
    """Lee el diario del día (lista de eventos); [] si no existe."""
    fichero = salida_fecha / "eventos.jsonl"
    if not fichero.exists():
        return []
    eventos = []
    for linea in fichero.read_text(encoding="utf-8").splitlines():
        if linea.strip():
            try:
                eventos.append(json.loads(linea))
            except json.JSONDecodeError:
                log.warning("Línea ilegible en el diario: %s", linea[:60])
    return eventos