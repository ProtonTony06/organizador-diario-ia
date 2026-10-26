"""Planifica el día: situación + inbox → Ollama → JSON validado del plan.

El modelo SOLO sugiere la organización (bloques horarios); esta función es
la única fuente de verdad del plan publicado: cada tarea referenciada debe
existir en el inbox, los bloques no pueden pisar anclas ni salirse de la
ventana, las horas de foco no pueden superar la capacidad configurada. Las
ANCLAS el modelo no las pone: las añade el pipeline (bloques fijos), y la
validación solo mira que sus bloques no las solapen. Lo inválido se
descarta y se re-pregunta con el error como pista (INTENTOS veces).
"""

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import yaml

import entrada as mod_entrada

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = RAIZ / "config" / "config.yaml"
RUTINAS = RAIZ / "config" / "rutinas.yaml"

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f]")
HORA = re.compile(r"^(\d{1,2}):(\d{2})$")

log = logging.getLogger("planificador")

# Parámetros de llamada idénticos al resumen (lecciones 2026-10-24):
# stdin DEVNULL (colgaba sin él) y --nowordwrap (el CLI corrompía
# cadenas largas re-envolviendo las líneas).
TIMEOUT_OLLAMA = 480.0

# El cloud de Ollama tira fallos TRANSITORIOS de red (p. ej. 502 Bad
# Gateway, hallado en producción en run real 2026-10-26: la mañana entera
# acabó en plan mecánico por DOS 502 seguidos — nadie reintentaba la
# LLAMADA, solo el JSON inválido). Reintentos con backoff corto y SOLO
# para esto: un timeout completo NO se reintenta (generación lenta, no
# red rota: repetir duplica coste). La excepción se lanza agotados ellos.
REINTENTOS_RED = 3
SEGS_BASE_RED = 20.0
MARCA_TRANSITORIA = (
    "502 ", "503 ", "504 ", "bad gateway", "service unavailable",
    "gateway timeout", "connection refused", "connection reset",
    "server error", "temporary failure", "eof",
)


def _limpiar(salida: str) -> str:
    """Quita escapes ANSI y caracteres de control del stdout de ollama."""
    return ANSI.sub("", salida)


def _quitar_puntitos(segmento: str) -> str:
    """Elimina los "..." literales FUERA de strings (imitación del prompt)."""
    salida, i, n = [], 0, len(segmento)
    en_string, escape = False, False
    while i < n:
        c = segmento[i]
        if en_string:
            salida.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_string = False
            i += 1
        elif c == '"':
            en_string = True
            salida.append(c)
            i += 1
        elif segmento.startswith("...", i):
            limpio = "".join(salida).rstrip()
            if limpio.endswith(","):
                limpio = limpio[:-1]
            salida = list(limpio)
            i += 3
        else:
            salida.append(c)
            i += 1
    return "".join(salida)


def _parsear_objeto(texto: str, inicio: int, fin: int):
    segmento = texto[inicio:fin]
    try:
        return json.loads(segmento)
    except (json.JSONDecodeError, ValueError):
        pass
    tolerado = _quitar_puntitos(segmento)
    if tolerado == segmento:
        return None
    try:
        return json.loads(tolerado)
    except (json.JSONDecodeError, ValueError):
        return None


def _extraer_jsons(texto: str) -> list:
    """Todos los objetos JSON balanceados del texto (el modelo razona antes)."""
    objetos = []
    for inicio, caracter in enumerate(texto):
        if caracter != "{":
            continue
        profundidad, en_string, escape = 0, False, False
        for i in range(inicio, len(texto)):
            c = texto[i]
            if en_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    en_string = False
            elif c == '"':
                en_string = True
            elif c == "{":
                profundidad += 1
            elif c == "}":
                profundidad -= 1
                if profundidad == 0:
                    objeto = _parsear_objeto(texto, inicio, i + 1)
                    if objeto is not None:
                        objetos.append(objeto)
                    break
    return objetos


def _elegir_objeto(texto: str, criterio) -> dict | None:
    """El ÚLTIMO objeto JSON balanceado que cumple el criterio (los
    borradores del razonamiento van antes). Compartido por plan y chat."""
    objetos = [o for o in _extraer_jsons(texto) if isinstance(o, dict)]
    con_clave = [o for o in objetos if criterio(o)]
    return con_clave[-1] if con_clave else None


def _elegir_respuesta(texto: str):
    """El ÚLTIMO objeto con clave "plan" de tipo lista (los borradores van antes)."""
    return _elegir_objeto(texto, lambda o: isinstance(o.get("plan"), list))


def llamar_ollama(prompt: str, modelo: str, timeout: float = TIMEOUT_OLLAMA) -> str:
    """Llama al CLI de ollama y devuelve stdout limpio (ver notas de resumen).

    Reintenta SOLO los fallos de red transitorios del cloud (502 y similares)
    con backoff corto; lo demás (timeout del propio llamar_ollama incluido)
    sigue siendo error inmediato: la validación/reintentos de nivel superior
    ya tienen su política y un timeout no es un tropezón de red.
    """
    for intento in range(1, REINTENTOS_RED + 1):
        try:
            resultado = subprocess.run(
                ["ollama", "run", modelo, "--think=false", "--format", "json", "--nowordwrap", prompt],
                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise
        if resultado.returncode == 0:
            return _limpiar(resultado.stdout)
        # El CLI mezcla el error con su spinner en stdout Y stderr; la
        # huella del 502 puede estar en cualquiera de los dos.
        ruido = (resultado.stderr + " " + resultado.stdout).lower()
        transitorio = any(marca in ruido for marca in MARCA_TRANSITORIA)
        if not transitorio or intento == REINTENTOS_RED:
            raise RuntimeError(
                f"ollama devolvió código {resultado.returncode}: "
                f"{resultado.stderr[-300:] or resultado.stdout[-300:]}"
            )
        log.warning(
            "Fallo transitorio de ollama (intento %d/%d), reintento en %ds: %s",
            intento, REINTENTOS_RED, int(SEGS_BASE_RED * intento),
            (resultado.stderr.strip() or resultado.stdout.strip())[-120:],
        )
        time.sleep(SEGS_BASE_RED * intento)
    raise RuntimeError("llamar_ollama agotó los reintentos de red")  # inalcanzable


def leer_config() -> dict:
    """Lee config.yaml (modelo, intentos) y rutinas.yaml (ventana, anclas...)."""
    with CONFIG.open(encoding="utf-8") as fichero:
        config = yaml.safe_load(fichero) or {}
    with RUTINAS.open(encoding="utf-8") as fichero:
        rutinas = yaml.safe_load(fichero) or {}
    config.setdefault("plan", {})
    return {
        "modelo": (config.get("ollama") or {}).get("modelo", "glm-5.3:cloud"),
        "intentos": int(config["plan"].get("intentos", 2)),
        "rutinas": rutinas,
    }


def _minutos(hora: str) -> int | None:
    """'07:30' → minutos desde las 00:00; horas < 6 cuentan como día siguiente.

    Rangos validados: minutos 0–59 y horas 0–23 (el cruce de medianoche va
    por las horas < 6, no por 25:70 — un modelo puede emitir horas absurdas
    y no deben pasar la validación).
    """
    m = HORA.match(hora.strip())
    if not m:
        return None
    minuto = int(m.group(1))
    segundo = int(m.group(2))
    if minuto > 23 or segundo > 59:
        return None
    valor = minuto * 60 + segundo
    if minuto < 6:
        valor += 24 * 60
    return valor


def _hhmm(minutos: int) -> str:
    minutos %= 24 * 60
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


def _anclas_mingan(rutinas: dict) -> list[tuple[int, int, str]]:
    """Anclas → [(inicio_min, fin_min, nombre)] con la misma normalización."""
    anclas = []
    for ancla in rutinas.get("anclas") or []:
        inicio = _minutos(str(ancla["momento"]))
        if inicio is None:
            continue
        anclas.append((inicio, inicio + int(ancla.get("duracion", 30)), ancla["nombre"]))
    return sorted(anclas)


PROMPT = """Eres el organizador del día de Antonio Cuevas (Sevilla, España).
Con tu lista de tareas pendientes y sus rutinas fijas, genera el plan para
HOY (fecha: {fecha}).

Responde EXCLUSIVAMENTE con un JSON (puedes envolverlo en vallas ```json)
con esta forma exacta:
{{
  "plan": [
    {{"inicio": "HH:MM", "fin": "HH:MM", "tipo": "foco|personal|descanso", "tarea": "<id|null>", "titulo": "<título corto>", "nota": "<opcional>"}}
  ],
  "no_caben": ["<id>", ...],
  "comentario": "<una frase sobre el plan>"
}}

Reglas obligatorias:
- Los bloques "foco" llevan UNA tarea del inbox (campo "tarea" con su id
  exacto). Los "personal" y "descanso" llevan "tarea": null y un "titulo".
- NO pongas las comidas ni snacks: el sistema las inserta solo. Tus bloques
  no pueden pisar estas horas fijas:
{lista_anclas}
- Los bloques no se solapan ENTRE SÍ; van dentro de la ventana
  {ventana_inicio}–{ventana_fin} (después de la medianoche sigue contando:
  un bloque de las 00:30 es del mismo día).
- Total del día por categoría respetando capacidades: {capacidades}.
- Estimaciones: cada bloque "foco" dura ~la estimación de su tarea; si una
  tarea no cabe este día, márcala en "no_caben" y no la planifiques.
- Prioriza las tareas con fecha límite ("antes:").
- Deja huecos de descanso: no rellenes toda la ventana de bloques.
- Ningún placeholder, NINGÚN "...", NINGÚN comentario fuera del JSON.
- La clave "plan" es exactamente esa y su valor una lista JSON válida.

Preferencias del usuario:
{preferencias}

Tareas pendientes del inbox (id [categoría] texto):
{tareas}"""


def _validar(respuesta: dict, tareas: list, rutinas: dict) -> list[str]:
    """Devuelve la lista de problemas del JSON del modelo ([] = válido).

    El modelo no pone las anclas (las añade el pipeline), así que la
    comprobación es: ids reales, dentro de ventana, sin solaparse entre sí
    ni con las ventas de ancla, y sin superar las capacidades de foco.
    """
    errores: list[str] = []
    por_id = {t.id: t for t in tareas}
    bloques = respuesta.get("plan") if isinstance(respuesta, dict) else None
    if not isinstance(bloques, list) or not bloques:
        return ["\"plan\" no es una lista con bloques"]

    def min_hora(bloque: dict, clave: str) -> int | None:
        valor = bloque.get(clave) if isinstance(bloque, dict) else None
        if not isinstance(valor, str):
            return None
        return _minutos(valor)

    def id_tarea(bloque: dict) -> str:
        """Id limpio del bloque (''/null/None → '')."""
        tid = bloque.get("tarea")
        tid = "" if tid is None else str(tid).strip()
        return "" if tid.lower() in ("", "null", "none") else tid

    normalizados = []
    for bloque in bloques:
        if not isinstance(bloque, dict):
            errores.append(f"elemento de \"plan\" no es un objeto: {str(bloque)[:60]}")
            continue
        inicio, fin = min_hora(bloque, "inicio"), min_hora(bloque, "fin")
        if inicio is None or fin is None:
            errores.append(f"bloque con hora ilegible: {json.dumps(bloque, ensure_ascii=False)[:80]}")
            continue
        if fin <= inicio:
            errores.append(f"bloque {_hhmm(inicio)}–{_hhmm(fin)} acaba antes de empezar")
            continue
        tid = id_tarea(bloque)
        if tid and tid not in por_id:
            errores.append(f"id de tarea desconocido en el inbox: {tid!r}")
        normalizados.append((inicio, fin, bloque, tid))

    ventana_inicio = _minutos(str((rutinas.get("ventana") or {}).get("inicio", "07:30")))
    ventana_fin = _minutos(str((rutinas.get("ventana") or {}).get("fin", "01:00")))
    if ventana_fin <= ventana_inicio:
        ventana_fin += 24 * 60
    anclas = _anclas_mingan(rutinas)

    # Orden cronológico: solapamientos entre los bloques del modelo y contra
    # las anclas (que el modelo no emite, pero no puede pisar).
    orden = sorted(normalizados, key=lambda b: (b[0], b[1]))
    fin_previo: int | None = None
    for inicio, fin, _bloque, _tid in orden:
        if fin_previo is not None and inicio < fin_previo:
            errores.append(f"bloques solapados alrededor de las {_hhmm(inicio)}")
            # I7 (2026-10-25): quedarse con el fin MÁS LARGO. Con la
            # reasignación directa una tarea ANIDADA (que acaba antes que la
            # que la contiene) encogía fin_previo y el bloque siguiente ya
            # no se comparaba contra el solape real.
            fin_previo = max(fin_previo, fin)
        else:
            fin_previo = fin
        if inicio < ventana_inicio or fin > ventana_fin:
            errores.append(f"bloque {_hhmm(inicio)}–{_hhmm(fin)} fuera de la ventana")
            continue
        for a_inicio, a_fin, nombre in anclas:
            if inicio < a_fin and a_inicio < fin:
                errores.append(f"bloque {_hhmm(inicio)}–{_hhmm(fin)} pisa la ancla {nombre}")

    # Regla de COHERENCIA tipo/tarea (hallazgo del test end-to-end real,
    # 2026-10-25: el modelo puso una tarea pendiente como bloque "personal"
    # con SOLO el título y se colaba — la web la listaba duplicada):
    # - un bloque "foco" DEBE enlazar un id de tarea del inbox;
    # - un bloque "personal"/"descanso" NO lleva tarea.
    for inicio, fin, bloque, tid in normalizados:
        tipo = str(bloque.get("tipo") or "foco").strip().lower()
        if tipo == "foco" and not tid:
            errores.append(
                f"bloque foco {_hhmm(inicio)}–{_hhmm(fin)} sin tarea: todo foco enlaza un id del inbox"
            )
        elif tipo in ("personal", "descanso") and tid:
            errores.append(
                f"bloque tipo {tipo} {_hhmm(inicio)}–{_hhmm(fin)} no puede llevar tarea "
                f"(el id {tid!r} va en un bloque foco o en no_caben)"
            )

    # Toda tarea pendiente del inbox debe acabar PLANIFICADA (en un bloque,
    # aunque esté duplicada: el duplicado se reporta aparte abajo) o
    # marcada en "no_caben" — planificarla por título sin id NO vale.
    no_caben_ids = {i for i in (respuesta.get("no_caben") or []) if isinstance(i, str)}
    planificados = {tid for _, _, _, tid in normalizados if tid}
    for tarea in tareas:
        if tarea.id not in planificados and tarea.id not in no_caben_ids:
            errores.append(f"la tarea {tarea.id} debe ir en un bloque foco o en \"no_caben\"")

    # Capacidad por categoría: minutos asignados a tareas de cada categoría.
    capacidades = rutinas.get("capacidades") or {}
    consumido: dict[str, int] = {}
    vistas: set[str] = set()
    for inicio, fin, _bloque, tid in normalizados:
        if not tid:
            continue
        # I5 (2026-10-25): el id repetido se reporta en CADA aparición
        # extra y NO se salta: siguen sumándose sus minutos, que es lo que
        # la capacidad de la categoría tiene que reflejar.
        if tid in vistas:
            errores.append(f"tarea {tid} aparece en más de un bloque")
        vistas.add(tid)
        # Un id desconocido YA se reportó al recorrer normalizados: aquí se
        # ignora (get, no [ ]), que sea la lista de errores y no un KeyError
        # el resultado de este bloque — un error de validación puede matar
        # el pipeline entero si sale como excepción (bug 2026-10-25).
        tarea = por_id.get(tid)
        if tarea is None:
            continue
        consumido[tarea.categoria] = consumido.get(tarea.categoria, 0) + (fin - inicio)
    for categoria, usado in consumido.items():
        limite = capacidades.get(categoria)
        if limite is not None and usado > int(limite):
            errores.append(
                f"categoría {categoria}: {usado}m asignados superan la "
                f"capacidad ({int(limite)}m)"
            )

    for tid in respuesta.get("no_caben") or []:
        if isinstance(tid, str) and tid not in por_id:
            errores.append(f"id desconocido en \"no_caben\": {tid!r}")
    return errores


def planificar(tareas: list, rutinas: dict, modelo: str, intentos: int = 2, fecha: str | None = None, desde: str | None = None) -> dict:
    """Reparte los bloques del día llamando a Ollama (JSON validado o error).

    `desde` (HH:MM) re-plancha el inicio de la ventana: es el re-plan del
    resto del día (las tareas cerradas ya no están en el inbox).
    """
    if desde:
        rutinas = dict(rutinas)
        rutinas["ventana"] = {**(rutinas.get("ventana") or {}), "inicio": desde}
    if not tareas:
        log.info("Inbox vacío: no hace falta llamar al modelo")
        return {"plan": [], "no_caben": [], "comentario": "Inbox vacío: día de rutinas.", "valido": True}
    ventana = rutinas.get("ventana") or {}
    lista_anclas = "\n".join(
        f'  - {a["nombre"]}: {a["momento"]}–{_hhmm(_minutos(str(a["momento"])) + int(a.get("duracion", 30)))} (OCUPADO, duración exacta {a.get("duracion", 30)} min)'
        for a in rutinas.get("anclas") or []
    )
    capacidades = " · ".join(f"{k}: {v} min/día" for k, v in (rutinas.get("capacidades") or {}).items())
    base = PROMPT.format(
        fecha=fecha or date.today().strftime("%Y-%m-%d"),
        lista_anclas=lista_anclas,
        ventana_inicio=str(ventana.get("inicio", "07:30")),
        ventana_fin=str(ventana.get("fin", "01:00")),
        capacidades=capacidades,
        preferencias="\n".join(f"- {p}" for p in rutinas.get("preferencias") or []),
        tareas=mod_entrada.a_prompt(tareas),
    )
    errores, respuesta = [], None
    for intento in range(1, max(intentos, 1) + 1):
        # El reintento SABE lo que pasó antes: errores del intento previo
        # van pegados al prompt (reprobar sin pistas repite el mismo fallo).
        prompt = base
        if errores:
            prompt = base + (
                "\n\nCORRECCIÓN OBLIGATORIA: tu plan anterior NO sirve por esto: "
                + "; ".join(errores)
                + ". Regenera el JSON completo corrigiendo SOLO esos puntos."
            )
        texto = llamar_ollama(prompt, modelo)
        respuesta = _elegir_respuesta(texto)
        if not isinstance(respuesta, dict) or not isinstance(respuesta.get("plan"), list):
            respuesta = None
            errores = ["la respuesta no lleva un JSON utilizable con la clave plan"]
            log.warning("Intento %d sin JSON utilizable", intento)
            continue
        errores = _validar(respuesta, tareas, rutinas)
        if not errores:
            break
        log.warning("Intento %d con %d problema(s) de validación: %s", intento, len(errores), "; ".join(errores))
    if respuesta is None:
        raise RuntimeError("Sin plan usable tras los reintentos")
    respuesta["valido"] = not errores
    if errores:
        respuesta["errores"] = errores
        raise RuntimeError("El plan viola las rutinas: " + "; ".join(errores))
    log.info("Plan válido: %d bloques", len(respuesta["plan"]))
    return respuesta


def plan_mecanico(tareas: list, rutinas: dict, desde: str | None = None) -> dict:
    """Plan de emergencia SIN modelo: colocación secuencial que respeta rutinas.

    Si Ollama no da un plan válido tras los reintentos, la mañana no se
    queda sin salida: se ordenan las pendientes (fecha límite primero,
    luego categoría) y se colocan en los huecos entre anclas, con su
    estimación (45m sin ella) y sin superar capacidades. Simple y verde;
    el comentario lo declara como plan mecánico.
    """
    capacidades = rutinas.get("capacidades") or {}
    ventana = rutinas.get("ventana") or {}
    inicio = _minutos(desde or str(ventana.get("inicio", "07:30")))
    fin_ventana = _minutos(str(ventana.get("fin", "01:00")))
    if fin_ventana <= inicio:
        fin_ventana += 24 * 60
    anclas = _anclas_mingan(rutinas)

    orden = sorted(
        tareas,
        key=lambda t: (t.antes or "9999-99-99", CATEGORIAS_ORDEN.get(t.categoria, 99), -(t.minutos or 45)),
    )
    bloques, no_caben = [], []
    consumido: dict[str, int] = {}
    cursor = inicio
    for tarea in orden:
        duracion = tarea.minutos or 45
        limite = capacidades.get(tarea.categoria)
        if limite is not None and consumido.get(tarea.categoria, 0) + duracion > int(limite):
            no_caben.append(tarea.id)
            continue
        # Primer hueco desde cursor que aguante la tarea sin pisar un ancla.
        puesto = False
        for a_inicio, a_fin, _nombre in anclas:
            inicio_hueco = cursor
            fin_hueco = a_inicio if a_inicio >= cursor else None
            if fin_hueco is not None and fin_hueco >= inicio_hueco + duracion:
                bloques.append(_bloque(inicio_hueco, inicio_hueco + duracion, tarea))
                consumido[tarea.categoria] = consumido.get(tarea.categoria, 0) + duracion
                cursor = inicio_hueco + duracion
                puesto = True
                break
            if a_inicio >= cursor:   # el hueco era corto: saltar tras la ancla
                cursor = max(cursor, a_fin)
        if not puesto:
            if cursor + duracion <= fin_ventana:
                bloques.append(_bloque(cursor, cursor + duracion, tarea))
                consumido[tarea.categoria] = consumido.get(tarea.categoria, 0) + duracion
                cursor += duracion
            else:
                no_caben.append(tarea.id)
    return {
        "plan": bloques,
        "no_caben": no_caben,
        "comentario": (
            "Plan MECÁNICO (el modelo no dio un plan válido): colocación "
            "secuencial respetando anclas y capacidades."
        ),
        "valido": True,
        "fallback": True,
    }


CATEGORIAS_ORDEN = {"dev": 0, "formacion": 1, "hogar": 2, "personal": 3}


def _bloque(inicio: int, fin: int, tarea) -> dict:
    return {"inicio": _hhmm(inicio), "fin": _hhmm(fin), "tipo": "foco", "tarea": tarea.id}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Prueba del planificador con un inbox.")
    parser.add_argument("--inbox", default=RAIZ / "data" / "inbox.md")
    parser.add_argument("--fecha")
    argumentos = parser.parse_args()
    config = leer_config()
    tareas = mod_entrada.leer(argumentos.inbox)
    print(json.dumps(
        planificar(tareas, config["rutinas"], config["modelo"], config["intentos"], argumentos.fecha),
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())