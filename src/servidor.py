"""Servidor web del organizador (F1): página de cierre + API + PWA.

Una app FastAPI pequeña que sirve:
- GET  /                       la página interactiva del plan (cierre de tareas)
- GET  /api/plan               el plan del día en JSON (bloques, no_caben, estado)
- POST /api/tarea/{id}/cerrar  marca la tarea hecha (timestamp) y lanza el
                               re-plan del resto del día en background
- GET  /manifest.webmanifest, /icon.svg, /sw.js   (PWA instalable)

El plan y el inbox leen de los mismos ficheros que el pipeline (data/inbox.md
y out/<fecha>/): el servidor no copia estado, solo consulta. El re-plan pasa
un pequeño estado en memoria (idle/ejecutando/listo/error) que la página
consulta al refrescarse (poll de 15 s: MVP honesto, sin SSE ni websockets).
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

import entrada
import pipeline
import registro

RAIZ = Path(__file__).resolve().parent.parent
PAGINA = RAIZ / "templates" / "web" / "index.html"
MANIFEST = RAIZ / "templates" / "web" / "manifest.webmanifest"
ICONO = RAIZ / "templates" / "web" / "icon.svg"
SW = RAIZ / "templates" / "web" / "sw.js"

log = logging.getLogger("servidor")

# Estado del re-plan en curso (el HTTP no puede bloquear 2 minutos de LLM).
# I3 (2026-10-25): `cambios` cuenta los cierres llegados DURANTE un re-plan
# (api_cerrar lo incrementa SIEMPRE): al hilo le sirven de aviso de que el
# inbox ya no es el que planeó.
ESTADO_REPLAN = {"estado": "idle", "desde": None, "error": None, "cambios": 0}
CANDADO = threading.Lock()

app = FastAPI(title="organizador-diario-ia", docs_url=None, redoc_url=None)


def _fecha_de_hoy() -> str:
    # B1 (2026-10-25): MISMA convención que pipeline.ejecutar/replanificar
    # (fecha local, restando un día antes de las 06:00) — con UTC en el
    # pipeline y local aquí, la carpeta divergía 00:00–01:00.
    return registro.fecha_de_plan()


def _carpeta_dia(fecha: str) -> Path:
    return pipeline.SALIDA / fecha


def _estado_replan() -> str:
    with CANDADO:
        return ESTADO_REPLAN["estado"]


def _replan_en_segundo_plano() -> None:
    def trabajo() -> None:
        try:
            # I3 (2026-10-25): capturo el snapshot de ids PENDIENTES aquí,
            # dentro del hilo y ANTES de la primera replanificación (sitio
            # más simple que api_cerrar: el hilo es el que lo compara, así
            # el snapshot nace junto al primer re-plan que lo consume, ya
            # bajo el mismo orden de cierre→ejecutando→disparo).
            pendientes = {t.id for t in entrada.leer(pipeline.INBOX)}
            intentos = 0
            while True:
                intentos += 1
                pipeline.replanificar()
                # Los cierres de DURANTE el re-plan (contador más el inbox
                # re-leído) invalidan el plan recién publicado: sin esto,
                # la tarea cerrada mientras ollama pensaba se planificaba
                # igual y la web la mostraba viva en un bloque.
                nuevos = {t.id for t in entrada.leer(pipeline.INBOX)}
                if nuevos == pendientes or intentos >= 3:
                    break
                log.info(
                    "Cierres llegados durante el re-plan (intento %d): re-planeo otra vez",
                    intentos,
                )
            with CANDADO:
                ESTADO_REPLAN["estado"] = "listo"
        except Exception as error:  # noqa: BLE001 — el error se muestra en la web
            log.error("Re-plan falló: %s", error)
            with CANDADO:
                ESTADO_REPLAN["estado"] = "error"
                ESTADO_REPLAN["error"] = str(error)[:200]

    hilo = threading.Thread(target=trabajo, daemon=True)
    hilo.start()


@app.get("/", response_class=HTMLResponse)
def pagina() -> str:
    return PAGINA.read_text(encoding="utf-8")


@app.get("/api/plan")
def api_plan() -> dict:
    """El plan del día con el estado VIVO de las tareas (leído del inbox)."""
    fecha = _fecha_de_hoy()
    fichero = _carpeta_dia(fecha) / "plan.json"
    if not fichero.exists():
        return {
            "fecha": fecha, "hay_plan": False,
            "mensaje": "Aún no hay plan para hoy (se genera a las 07:00 o con el pipeline).",
            "replan": _estado_replan(),
        }
    plan = json.loads(fichero.read_text(encoding="utf-8"))
    todas = {t.id: t for t in entrada.leer_todas(pipeline.INBOX)}
    pendientes = {tid for tid, t in todas.items() if not t.hecha}
    bloques = []
    for bloque in plan.get("bloques", []):
        tid = bloque.get("tarea") or ""
        tarea = todas.get(tid)
        bloques.append({
            **bloque,
            "planificada": bool(tid),
            "cerrada": bool(tid) and tid not in pendientes,
            "texto_tarea": tarea.texto if tarea is not None else bloque.get("titulo", ""),
        })
    pendientes_fuera = [
        t for t in entrada.leer(pipeline.INBOX)
        if t.id not in {b.get("tarea") for b in plan["bloques"] if b.get("tarea")}
        and t.id not in {n["id"] for n in plan.get("no_caben", [])}
    ]
    return {
        "fecha": fecha, "hay_plan": True,
        "generado": plan.get("generado"), "comentario": plan.get("comentario"),
        "bloques": bloques,
        "no_caben": plan.get("no_caben", []),
        "pendientes_sin_bloque": [
            {"id": t.id, "categoria": t.categoria, "texto": t.texto} for t in pendientes_fuera
        ],
        "replan": _estado_replan(),
    }


@app.post("/api/tarea/{id_tarea}/cerrar")
def api_cerrar(id_tarea: str) -> dict:
    fecha = _fecha_de_hoy()
    tareas = entrada.leer(pipeline.INBOX)
    ok = registro.cerrar_en_inbox(pipeline.INBOX, id_tarea)
    if not ok:
        raise HTTPException(status_code=404, detail="Tarea no encontrada en el inbox")
    tarea = next((t for t in tareas if t.id == id_tarea), None)
    registro.registrar_evento(_carpeta_dia(fecha), "cierre", {
        "id_tarea": id_tarea,
        "texto": tarea.texto if tarea else id_tarea,
        "categoria": tarea.categoria if tarea else "",
    })
    with CANDADO:
        # I3 (2026-10-25): el contador incrementa SIEMPRE — aunque haya un
        # re-plan en marcha, este cierre tiene que quedar registrado (la
        # comparación de ids del hilo re-planea si el conjunto cambió).
        ESTADO_REPLAN["cambios"] = int(ESTADO_REPLAN.get("cambios") or 0) + 1
        if ESTADO_REPLAN["estado"] != "ejecutando":
            ESTADO_REPLAN["estado"] = "ejecutando"
            ESTADO_REPLAN["desde"] = datetime.now().strftime("%H:%M")
            ESTADO_REPLAN["error"] = None
            disparar = True
        else:
            disparar = False  # ya hay un re-plan en marcha: cubre este cierre
    if disparar:
        _replan_en_segundo_plano()
    return {"ok": True, "replan": _estado_replan()}


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/icon.svg")
def icono() -> FileResponse:
    return FileResponse(ICONO, media_type="image/svg+xml")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(SW, media_type="application/javascript")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8010, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())