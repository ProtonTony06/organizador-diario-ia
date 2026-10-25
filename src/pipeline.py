"""Orquestador del organizador del día: inbox → plan → MD + HTML → notificar.

Flujo (F0): leer inbox (data/inbox.md o el que se pase) → planificar con
Ollama (JSON validado contra rutinas e inbox) → Markdown del día (anclas
insertadas aquí, no por el modelo) → plan.html + índice → notificación
ntfy. Salidas en out/<fecha>/. Hermano del pipeline de resumen-diario-ia.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import adaptador_html
import entrada
import notificaciones
import plan_md
import planificador

RAIZ = Path(__file__).resolve().parent.parent
INBOX = RAIZ / "data" / "inbox.md"
SALIDA = RAIZ / "out"

log = logging.getLogger("pipeline")


def ejecutar(fecha: str | None = None, inbox: Path = INBOX, notificar: bool = True) -> int:
    fecha = fecha or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    carpeta = SALIDA / fecha
    log.info("Pipeline del %s → %s", fecha, carpeta)

    # 1) Inbox (la única entrada de tareas; la captura es F0, edición manual)
    tareas = entrada.leer(inbox)
    log.info("Inbox: %d tarea(s) pendiente(s)", len(tareas))

    # 2) Planificación con Ollama (JSON validado contra rutinas e inbox)
    config = planificador.leer_config()
    log.info("Planificando con modelo %s (puede tardar unos minutos)…", config["modelo"])
    resultado = planificador.planificar(
        tareas, config["rutinas"], config["modelo"], config["intentos"], fecha,
    )

    # 3) Markdown del día (la base de verdad: anclas insertadas aquí)
    ruta_md = carpeta / "plan.md"
    ruta_md.parent.mkdir(parents=True, exist_ok=True)
    ruta_md.write_text(plan_md.a_markdown(resultado, tareas, fecha, config["rutinas"]), encoding="utf-8")
    log.info("Plan guardado en %s", ruta_md)

    # 4) Presentación + índice del historial
    adaptador_html.md_a_html(ruta_md)
    adaptador_html.generar_indice(SALIDA)

    # 5) Notificación (titular: bloques y tareas planificadas)
    n_bloques = len(resultado.get("plan") or [])
    n_tareas = sum(1 for t in tareas if any(b.get("tarea") == t.id for b in resultado.get("plan") or []))
    sin_hueco = len(resultado.get("no_caben") or [])
    titular = f"Plan listo: {n_bloques} bloques, {n_tareas} tareas"
    if sin_hueco:
        titular += f" ({sin_hueco} sin hueco)"
    if notificar:
        notificaciones.avisar(titular, notificaciones.url_del_dia(fecha))

    log.info("Pipeline terminado en %s", carpeta)
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
    argumentos = parser.parse_args()
    if argumentos.listar:
        listar(argumentos.inbox)
        return 0
    try:
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