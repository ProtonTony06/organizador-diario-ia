"""Servidor web del organizador (F1): página de cierre + API + PWA.

Una app FastAPI pequeña que sirve:
- GET  /                       la página interactiva del plan (cierre de tareas)
- GET  /api/plan               el plan del día en JSON (bloques, no_caben, estado)
- POST /api/tarea/{id}/cerrar  marca la tarea hecha (timestamp) y lanza el
                               re-plan del resto del día en background
- POST /api/chat               un turno del chat de contexto (F2, fase de
                               aprendizaje): guarda el mensaje, pide la
                               respuesta al modelo en background y aplica
                               registrar/aprender (NO toca la planificación)
- GET  /api/chat               estado del turno en curso, error si lo hubo y
                               la conversación de hoy (chat.jsonl)
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

import chat
import entrada
import pipeline
import planificador
import registro
from pydantic import BaseModel

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

# Estado del turno del chat en curso (F2, fase de aprendizaje): el HTTP no
# puede bloquear en la llamada al modelo, así que POST lanza el turno en un
# hilo y la web hace poll de GET /api/chat (mismo MVP que el re-plan). Un
# turno cada vez: 409 si ya hay uno pensando (sin colas, sin solapamientos).
ESTADO_CHAT = {"estado": "idle", "error": None}
CANDADO_CHAT = threading.Lock()

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


def _estado_chat() -> dict:
    with CANDADO_CHAT:
        estado = {
            "estado": ESTADO_CHAT["estado"],
            "error": ESTADO_CHAT["error"],
        }
    # La conversación se lee FUERA del candado: chat.jsonl ya se protege
    # solo (flock en guardar_sala) y el disco no debe esperar a los turnos.
    estado["mensajes"] = chat.leer_chat(_carpeta_dia(_fecha_de_hoy()))
    return estado


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


def _chat_en_segundo_plano(texto: str, carpeta: Path) -> None:
    """El turno del chat fuera del request (el modelo puede tardar minutos).

    La conversación vive en chat.jsonl (chat.leer_chat/guardar_sala): el
    hilo solo añade mensajes y deja el estado en memoria para el poll. Si
    algo falla (ollama caído, JSON inválido tras los reintentos…), el error
    se guarda también como mensaje de la sala: la conversación de hoy
    queda completa y la web puede mostrar el porqué, sin dejar a Antonio
    atascado esperando una respuesta que no va a llegar.

    Si guardar el MENSAJE de error volviera a fallar (disco lleno…), no
    puede colgarse el turno: el estado se repone SIEMPRE (el "pensando"
    eterno sería un 409 sin fin: el chat quedaría acéfalo hasta reinicio).
    """
    def trabajo() -> None:
        try:
            config = planificador.leer_config()
            # Los intentos de validación viven en el config (leer_config/plan).
            resultado = chat.responder(
                texto, config["rutinas"], config["modelo"], intentos=config["intentos"],
            )
            hechos = chat.aplicar(resultado, carpeta)
            chat.guardar_sala(
                carpeta, "agente", resultado["respuesta"],
                acciones=[{"hecho": k, "texto": resultado[k]}
                          for k in ("registrar", "aprender") if resultado[k]],
            )
            log.info("Turno del chat aplicado: %s", hechos or "(sin acciones)")
            with CANDADO_CHAT:
                ESTADO_CHAT["estado"] = "listo"
        except Exception as error:  # noqa: BLE001 — el error se muestra en la web
            log.error("Turno del chat falló: %s", error)
            detalle = str(error)[:200]
            try:
                chat.guardar_sala(carpeta, "error", detalle)
            except Exception as fallo_sala:
                log.error("No se pudo guardar el error en la sala: %s", fallo_sala)
            with CANDADO_CHAT:
                ESTADO_CHAT["estado"] = "error"
                ESTADO_CHAT["error"] = detalle

    hilo = threading.Thread(target=trabajo, daemon=True)
    hilo.start()


class MensajeChat(BaseModel):
    """Body de POST /api/chat: lo que Antonio escribe en el chat."""

    texto: str


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


@app.post("/api/chat")
def api_chat(mensaje: MensajeChat) -> dict:
    """Un turno del chat (fase de aprendizaje): escucha, responde y aprende."""
    texto = mensaje.texto.strip()
    if not texto:
        raise HTTPException(status_code=400, detail="El mensaje del chat no puede ir vacío")
    fecha = _fecha_de_hoy()
    carpeta = _carpeta_dia(fecha)
    with CANDADO_CHAT:
        if ESTADO_CHAT["estado"] == "pensando":
            raise HTTPException(
                status_code=409,
                detail="Ya hay un turno del chat en marcha (espera la respuesta)",
            )
        ESTADO_CHAT["estado"] = "pensando"
        ESTADO_CHAT["error"] = None
    try:
        # Entre la marca y el hilo nada puede fallar "a medias": si el
        # mensaje no llega a la sala o el hilo no arranca, el turno nunca
        # empezó y el estado no puede quedarse en "pensando" (409 eterno:
        # el chat quedaría acéfalo hasta reiniciar el servidor).
        chat.guardar_sala(carpeta, "antonio", texto)
        _chat_en_segundo_plano(texto, carpeta)
    except Exception as error:  # noqa: BLE001 — fallo síncrono, se cuenta
        log.error("No se pudo arrancar el turno del chat: %s", error)
        with CANDADO_CHAT:
            ESTADO_CHAT["estado"] = "error"
            ESTADO_CHAT["error"] = str(error)[:200]
        raise HTTPException(
            status_code=500,
            detail="No se pudo arrancar el turno del chat: " + str(error)[:200],
        ) from error
    return {"ok": True, "chat": _estado_chat()}


@app.get("/api/chat")
def api_chat_estado() -> dict:
    """Estado del turno en curso, error y conversación de hoy (chat.jsonl)."""
    return _estado_chat()


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