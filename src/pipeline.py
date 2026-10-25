"""Orquestador del organizador del día: inbox → plan → MD + HTML → notificar.

Flujo (F0): leer inbox (data/inbox.md o el que se pase) → planificar con
Ollama (JSON validado contra rutinas e inbox) → Markdown del día (anclas
insertadas aquí, no por el modelo) → plan.html + índice → notificación
ntfy. Salidas en out/<fecha>/. Hermano del pipeline de resumen-diario-ia.
"""

import argparse
import fcntl
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import adaptador_html
import entrada
import notificaciones
import plan_md
import planificador
import registro

RAIZ = Path(__file__).resolve().parent.parent
INBOX = RAIZ / "data" / "inbox.md"
SALIDA = RAIZ / "out"
# I4 (2026-10-25): candado de pipeline (nunca se borra) — un CLI manual a
# mano puede pisar el thread del servidor (o al revés) en plena publicación.
LOCK_PIPELINE = RAIZ / "data" / "pipeline.lock"

log = logging.getLogger("pipeline")


@contextmanager
def _candado_pipeline():
    """flock EXCLUSIVO y bloqueante del pipeline: solo un plan a la vez."""
    LOCK_PIPELINE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PIPELINE.open("a", encoding="utf-8") as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(candado, fcntl.LOCK_UN)


def _escribir_atomico(destino: Path, contenido: str) -> None:
    """I4: escribe primero a `<nombre>.tmp` y renombra con os.replace.

    Un lector (la web lee plan.json sin candado) nunca ve un fichero a
    medias: o el contenido entero anterior, o el entero nuevo (os.replace
    es atómico en POSIX). El .tmp se crea a renglón seguido del destino.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_name(destino.name + ".tmp")
    temporal.write_text(contenido, encoding="utf-8")
    os.replace(temporal, destino)


def _escribir_json(datos: dict, destino: Path) -> None:
    _escribir_atomico(destino, json.dumps(datos, ensure_ascii=False, indent=2))


def _publicar(resultado: dict, tareas: list, fecha: str, rutinas: dict, carpeta: Path, generado: str | None = None) -> Path:
    """Escribe plan.md + plan.json + plan.html + índice. Devuelve el plan.json.

    Solo el re-plan pasa `generado` (la hora, que va anotada en el MD).
    plan.json es la vista machine-readable que consumirá la web de F1 (el
    MD es la base de verdad; el JSON el espejo estructurado del MISMO plan).
    """
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_md = carpeta / "plan.md"
    _escribir_atomico(
        ruta_md,
        plan_md.a_markdown(resultado, tareas, fecha, rutinas, generado=generado),
    )
    plan_json = {
        "fecha": fecha,
        "generado": generado or datetime.now().strftime("%H:%M"),
        "comentario": str(resultado.get("comentario") or ""),
        "bloques": [
            {**b, "inicio": plan_md.hhmm(b["inicio"]), "fin": plan_md.hhmm(b["fin"])}
            for b in plan_md.bloques_del_dia(resultado, rutinas)
        ],
        "no_caben": [
            {"id": t.id, "categoria": t.categoria, "texto": t.texto}
            for t in tareas
            if any(i == t.id for i in (resultado.get("no_caben") or []))
        ],
    }
    _escribir_json(plan_json, carpeta / "plan.json")
    adaptador_html.md_a_html(ruta_md)
    adaptador_html.generar_indice(SALIDA)
    return carpeta / "plan.json"


def _planificar_con_fallback(tareas: list, config: dict, fecha: str, carpeta: Path, desde: str | None = None) -> dict:
    """Planifica y, si el modelo no da plan válido, usa el mecánico.

    La red de seguridad es obligatoria: el timer de las 07:00 no puede
    quedarse sin plan porque el modelo se empeñe en pisar la comida.
    """
    try:
        return planificador.planificar(
            tareas, config["rutinas"], config["modelo"], config["intentos"], fecha, desde=desde,
        )
    except Exception as fallo:  # noqa: BLE001 — aquí todo acaba en plan mecánico
        # I1 (2026-10-25): el except estrecho (RuntimeError, TimeoutError)
        # no pillaba subprocess.TimeoutExpired (subprocess.SubprocessError,
        # que no es OSError) ni FileNotFoundError si el binario de ollama
        # no existe: en ambos casos el pipeline moría SIN PLAN. La red de
        # seguridad no puede dejar escapar nada: cualquier fallo de
        # planificación cae al plan mecánico.
        log.warning("El modelo no dio un plan válido (%s): PLAN MECÁNICO", fallo)
        registro.registrar_evento(carpeta, "fallback_mecanico", {"motivo": str(fallo)[:200]})
        return planificador.plan_mecanico(tareas, config["rutinas"], desde=desde)


def ejecutar(fecha: str | None = None, inbox: Path = INBOX, notificar: bool = True) -> int:
    # B1 (2026-10-25): la etiqueta del día es la fecha de plan LOCAL (ver
    # registro.fecha_de_plan); el UTC de antes divergía del servidor a las
    # 00:00–01:00 locales.
    fecha = fecha or registro.fecha_de_plan()
    carpeta = SALIDA / fecha
    log.info("Pipeline del %s → %s", fecha, carpeta)

    # 1) Inbox (la única entrada de tareas; la captura es F0, edición manual)
    tareas = entrada.leer(inbox)
    log.info("Inbox: %d tarea(s) pendiente(s)", len(tareas))

    # 2) Planificación con Ollama (JSON validado contra rutinas e inbox)
    with _candado_pipeline():
        config = planificador.leer_config()
        log.info("Planificando con modelo %s (puede tardar unos minutos)…", config["modelo"])
        resultado = _planificar_con_fallback(tareas, config, fecha, carpeta)

        # 3-4) Publicación: MD (base de verdad) + JSON + HTML + índice
        _publicar(resultado, tareas, fecha, config["rutinas"], carpeta)
    log.info("Plan guardado en %s", carpeta)

    # 5) Notificación (titular: bloques y tareas planificadas)
    n_bloques = len(resultado.get("plan") or [])
    n_tareas = sum(1 for t in tareas if any(b.get("tarea") == t.id for b in resultado.get("plan") or []))
    sin_hueco = len(resultado.get("no_caben") or [])
    titular = f"Plan listo: {n_bloques} bloques, {n_tareas} tareas"
    if resultado.get("fallback"):
        titular += " (plan mecánico)"
    if sin_hueco:
        titular += f" ({sin_hueco} sin hueco)"
    if notificar:
        notificaciones.avisar(titular, notificaciones.url_del_dia(fecha))

    log.info("Pipeline terminado en %s", carpeta)
    return 0


def replanificar(fecha: str | None = None, inbox: Path = INBOX, notificar: bool = False) -> int:
    """Re-plan del resto del día: ventana desde AHORA, sin las tareas hechas.

    El plan de la mañana se sobreescribe (el MD lleva la nota del re-plan) y
    el cambio queda en el diario del día (eventos.jsonl) para la F2.
    Llamado por el servidor web al cerrar tareas; también usable a mano.
    """
    # B1 (2026-10-25): misma etiqueta local que ejecutar y el servidor.
    fecha = fecha or registro.fecha_de_plan()
    carpeta = SALIDA / fecha
    with _candado_pipeline():
        tareas = entrada.leer(inbox)
        desde = datetime.now().strftime("%H:%M")
        registro.registrar_evento(carpeta, "re-plan_solicitado", {"desde": desde, "pendientes": len(tareas)})
        config = planificador.leer_config()
        resultado = _planificar_con_fallback(tareas, config, fecha, carpeta, desde=desde)
        _publicar(resultado, tareas, fecha, config["rutinas"], carpeta, generado=desde)
        n_bloques = len(resultado.get("plan") or [])
        registro.registrar_evento(carpeta, "re-plan_generado", {"desde": desde, "bloques": n_bloques})
        if notificar:
            notificaciones.avisar(
                f"Re-plan a las {desde}: {n_bloques} bloques para lo que queda",
                notificaciones.url_del_dia(fecha),
            )
    log.info("Re-plan del %s guardado (desde %s, %d bloques)", fecha, desde, n_bloques)
    return 0


def listar(inbox: Path = INBOX) -> None:
    """Imprime el inbox parseado (para verificar la captura sin planificar)."""
    for tarea in entrada.leer(inbox):
        estimado = f" ~{tarea.minutos}m" if tarea.minutos else ""
        antes = f" · antes {tarea.antes}" if tarea.antes else ""
        print(f"{tarea.id} [{tarea.categoria}] {tarea.texto}{estimado}{antes}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Orquestador: inbox → plan del día → publicar → avisar.",
    )
    parser.add_argument("--fecha", metavar="YYYY-MM-DD", help="etiqueta del plan (hoy por defecto)")
    parser.add_argument("--inbox", type=Path, default=INBOX)
    parser.add_argument("--no-notificar", action="store_true")
    parser.add_argument("--listar", action="store_true", help="solo imprime el inbox parseado")
    parser.add_argument("--replan", action="store_true", help="re-plan del resto del día (desde AHORA)")
    argumentos = parser.parse_args()
    if argumentos.listar:
        listar(argumentos.inbox)
        return 0
    try:
        if argumentos.replan:
            return replanificar(argumentos.fecha, inbox=argumentos.inbox)
        return ejecutar(
            argumentos.fecha, inbox=argumentos.inbox, notificar=not argumentos.no_notificar,
        )
    except KeyboardInterrupt:
        log.warning("Pipeline interrumpido")
        return 130
    except Exception as error:  # noqa: BLE001 — el fallo se reporta, no se oculta
        log.error("Fallo del pipeline: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())