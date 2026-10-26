"""Chat de contexto (F2): Antonio cuenta qué hace; el agente escucha y aprende.

FASE DE APRENDIZAJE (decisión 2026-10-26): durante 1–2 semanas el chat NO
toca la planificación del día (sin cerrar tareas, sin re-plans). Su trabajo:
1. REGISTRAR los hechos objetivos del día ("me pongo a estudiar") en el
   diario del día (eventos.jsonl, el mismo de F1).
2. COMPRENDER el porqué: pregunta MÁS QUÉ UNA cosa por turno, solo si hace
   falta; no interrogatorio (la fatiga mata este tipo de apps).
3. APRENDER rutina: patrones estables → data/perfil.md (líneas acumulativas
   con fecha). Con semanas de chats, ese perfil será lo que el planificador
   matutino consuma (integración decidida más adelante).

Contrato con el modelo, IGUAL que el planificador: responde SOLO JSON, lo
inválido se descarta y se re-pregunta con el error como pista (INTENTOS).

{"respuesta": "<lo que le dices a Antonio>", "registrar": "<hecho|null>",
 "aprender": "<rutina aprendida|null>"}
"""

import fcntl
import json
import logging
import re
from datetime import date
from pathlib import Path

import registro

import pipeline
import planificador

RAIZ = Path(__file__).resolve().parent.parent
PERFIL = RAIZ / "data" / "perfil.md"

log = logging.getLogger("chat")

INTENTOS = 2

# El modelo puede razonar antes del JSON (igual que el planificador: se
# extraen los objetos balanceados y se queda el último válido).
PROMPT = """Eres el compañero de rutina de Antonio Cuevas (Sevilla, España).
Ahora mismo son las {hora} del {fecha}. Antonio te acaba de escribir un
mensaje: te cuenta lo que está haciendo, cómo le va o qué le ha pasado.

Estamos en FASE DE APRENDIZAJE de tu rutina: tu trabajo es conocer a
Antonio, NO planificar. No menciones el plan del día ni sugieras tareas
ni re-planifiques: solo escucha, comprende y aprende.

Responde EXCLUSIVAMENTE con un JSON (puedes envolverlo en vallas ```json)
con esta forma exacta:
{{
  "respuesta": "<lo que le dices a Antonio, conversacional y corto (1-4 frases)>",
  "registrar": "<hecho objetivo de su día en 3ª persona y corto, o null>",
  "aprender": "<patrón de rutina/preferencia estable que acabas de aprender, o null>"
}}

Reglas obligatorias:
- "respuesta" es tu conversación con Antonio. En plural de porqués:
  si el mensaje deja a la vista QUE hace algo pero no POR QUÉ o dónde
  encaja en su día, puedes hacer UNA pregunta — MÁXIMO UNA por turno, y
  NINGUNA si el mensaje ya se entendió (no preguntes por preguntar).
- "registrar" SOLO un hecho objetivo y ya confirmado de HOY ("Empieza a
  estudiar bases de datos a las 16:30"). Nunca suposiciones ni cosas que
  Antonio no haya dicho. null si no hay hecho nuevo.
- "aprender" SOLO cuando del mensaje (+ lo que ya sabes del perfil)
  sacas un patrón ESTABLE de su rutina, p. ej. "Estudia mejor por la
  mañana" o "Una llamada de su padre le saca del estudio". Nada puntual:
  eso va en "registrar". null si no hay patrón nuevo. Reescribe cada
  patrón en UNA frase.
- Ningún placeholder, NINGÚN "...", NINGÚN comentario fuera del JSON.

Fijas de su rutina (comidas y otros, NO hablan de plan, ya lo vive):
{rutinas}

Lo que ya sabes de Antonio (perfil acumulado de días anteriores):
{perfil}

Hoy ya ha pasado esto (diario de eventos):
{eventos}

Conversación de hoy hasta ahora:
{conversacion}

Mensaje nuevo de Antonio:
{mensaje}"""


def _hhmm_rutinas(rutinas: dict) -> str:
    """Anchas de rutinas.yaml → lista compacta para el prompt (contexto
    de su día-vivido: comidas y horarios fijos; NO es un plan, no se discute)."""
    lineas = []
    for ancla in rutinas.get("anclas") or []:
        inicio = planificador._minutos(str(ancla["momento"]))
        if inicio is None:
            continue
        fin = inicio + int(ancla.get("duracion", 30))
        lineas.append(
            f"  - {ancla['nombre']}: {ancla['momento']}–{planificador._hhmm(fin)}"
        )
    return "\n".join(lineas) if lineas else "- (ninguna)"


def leer_perfil(ruta: Path = PERFIL) -> list[str]:
    """Las líneas de aprendizaje del perfil ([] si aún no existe)."""
    if not ruta.exists():
        return []
    lineas = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        limpio = linea.strip()
        if limpio and not limpio.startswith("#"):
            lineas.append(limpio)
    return lineas


def guardar_aprendizaje(texto: str, ruta: Path = PERFIL, momento=None) -> bool:
    """Añade un patrón al perfil: `- (YYYY-MM-DD) texto`. True si lo guardó.

    Con flock (como el inbox): la escritura llega del hilo del chat en
    FastAPI y varios turnos pueden solaparse. Duplicado exacto → no lo
    añade (True igualmente: aprenderlo dos veces no es un error).
    """
    texto = texto.strip()
    if not texto:
        return False
    fecha = registro.fecha_de_plan(momento)
    linea = f"- ({fecha}) {texto}"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    # Cabezera si el fichero no existía aún (a mano es legible).
    tiene = ruta.exists()
    candado = ruta.with_suffix(".lock")
    with candado.open("a", encoding="utf-8") as fichero_candado:
        fcntl.flock(fichero_candado, fcntl.LOCK_EX)
        try:
            # Duplicado LINEA A LINEA, no por subcadena: "- (fecha) Estudia"
            # es subcadena de "- (fecha) Estudia mejor por la mañana" y el
            # `in` del texto entero daba por aprendido el patrón nuevo.
            ya = tiene and any(
                existente.strip() == linea for existente in ruta.read_text(encoding="utf-8").splitlines()
            )
            if not ya:
                with ruta.open("a", encoding="utf-8") as fichero:
                    if not tiene:
                        fichero.write("# Perfil de rutina de Antonio (lo aprendido del chat)\n\n")
                    fichero.write(linea + "\n")
        finally:
            fcntl.flock(fichero_candado, fcntl.LOCK_UN)
    return True


def leer_chat(salida_fecha: Path) -> list[dict]:
    """La conversación de hoy (chat.jsonl); [] si no existe."""
    fichero = salida_fecha / "chat.jsonl"
    if not fichero.exists():
        return []
    mensajes = []
    for linea in fichero.read_text(encoding="utf-8").splitlines():
        if linea.strip():
            try:
                mensajes.append(json.loads(linea))
            except json.JSONDecodeError:
                log.warning("Línea ilegible en el chat: %s", linea[:60])
    return mensajes


def guardar_sala(salida_fecha: Path, autor: str, texto: str, **extras) -> None:
    """Añade un mensaje a chat.jsonl (flock: escribe el endpoint y el hilo)."""
    if not str(texto).strip():
        return
    texto = str(texto).strip()
    mensaje = {"ts": registro.ahora().isoformat(timespec="seconds"),
               "autor": autor, "texto": texto}
    if extras:
        mensaje.update(extras)
    fichero = salida_fecha / "chat.jsonl"
    fichero.parent.mkdir(parents=True, exist_ok=True)
    candado = salida_fecha / "chat.lock"
    with candado.open("a", encoding="utf-8") as fichero_candado:
        fcntl.flock(fichero_candado, fcntl.LOCK_EX)
        try:
            with fichero.open("a", encoding="utf-8") as f:
                f.write(json.dumps(mensaje, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(fichero_candado, fcntl.LOCK_UN)


def _elegir_respuesta(texto: str):
    """El ÚLTIMO objeto con "respuesta" no vacía (+ razonamiento antes)."""
    if not isinstance(texto, str):
        return None
    return planificador._elegir_objeto(
        texto,
        lambda o: isinstance(o.get("respuesta"), str) and bool(o["respuesta"].strip()),
    )


def _validar(respuesta: dict) -> list[str]:
    """Problemas del JSON del modelo ([] = válido). Igual filosofía dura.

    SOLO dos campos de acción admitidos y SOLO de la fase de aprendizaje:
    "cerrar"/"posponer"/"plan" NO existen en este chat (decisión de 1–2
    semanas de escucha) — si el modelo los emite, inválido. Valida una
    COPIA cuando normaliza (mutar la entrada del llamador sería un efecto
    lateral que nadie espera de una función de validación).
    """
    if not isinstance(respuesta, dict):
        return ["la respuesta no es un objeto JSON"]
    if not str(respuesta.get("respuesta") or "").strip():
        return ["falta \"respuesta\" (lo que le dices a Antonio, no puede ir vacío)"]
    errores = []
    # Copia para normalizar los "null" COMO TEXTO: la validación no debía
    # mutar el dict del llamador (los reintentos pasan el MISMO elegido y
    # la mutación sembraba resultados viejos en él).
    respuesta = dict(respuesta)
    for campo in ("registrar", "aprender"):
        valor = respuesta.get(campo)
        if valor is not None and not isinstance(valor, str):
            errores.append(f"\"{campo}\" debe ser texto o null")
            continue
        if isinstance(valor, str):
            limpio = valor.strip()
            if limpio.lower() in ("", "null", "none", "n/a", "-"):
                respuesta[campo] = None
                continue
            if len(limpio) < 8:
                errores.append(f"\"{campo}\" demasiado corto para ser un hecho/patrón: {limpio!r}")
    for campo in list(respuesta):
        if campo not in ("respuesta", "registrar", "aprender"):
            errores.append(f"campo desconocido: {campo!r} (solo respuesta/registrar/aprender)")
    return errores


def _normalizar(respuesta: dict) -> dict:
    """Respuesta validada → campos finales (cortes y null por defecto).

    Los "null"/"none" como TEXTO van a None aquí (la mutación que hacía
    _validar ya no existe): el llamador nunca ve un registrar='null'.
    """
    def _opcional(valor):
        if not isinstance(valor, str):
            return None
        limpio = valor.strip()
        return None if limpio.lower() in ("", "null", "none", "n/a", "-") else limpio
    return {
        "respuesta": str(respuesta["respuesta"]).strip(),
        "registrar": _opcional(respuesta.get("registrar")),
        "aprender": _opcional(respuesta.get("aprender")),
    }


def construir_prompt(
    texto: str,
    carpeta_dia: Path,
    fecha: str,
    rutinas: dict,
    max_mensajes: int = 24,
    perfil: Path = PERFIL,
) -> str:
    """El prompt completo del turno: contexto + conversación + el mensaje."""
    hora = registro.ahora().strftime("%H:%M")
    perfil_texto = "\n".join(f"  {l}" for l in leer_perfil(perfil)[-30:]) or "  (aún no hay nada aprendido)"
    eventos = "\n".join(
        f"  {str(e.get('ts', ''))[:16]}: {e.get('tipo')} — "
        + " · ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "tipo"))
        for e in registro.leer_eventos(carpeta_dia)[-20:]
    ) or "  (nada registrado aún hoy)"
    mensajes = leer_chat(carpeta_dia)[-max_mensajes:]
    # El mensaje nuevo va aparte en {mensaje}: el endpoint lo guarda en la
    # sala ANTES de arrancar el turno, así que la cola del {conversacion}
    # lo trae ya — si no se recorta, el modelo lee el mismo texto por
    # duplicado (dos veces por reintento, encima).
    while mensajes and mensajes[-1]["autor"] == "antonio" and mensajes[-1]["texto"] == texto.strip():
        mensajes.pop()
    conversacion = "\n".join(
        f"  {m['ts'][11:16]} {m['autor'].upper()}: {m['texto']}" for m in mensajes
    ) or "  (primera conversación de hoy)"
    return PROMPT.format(
        hora=hora, fecha=fecha,
        rutinas=_hhmm_rutinas(rutinas),
        perfil=perfil_texto, eventos=eventos, conversacion=conversacion,
        mensaje=texto.strip(),
    )


def responder(
    texto: str,
    rutinas: dict,
    modelo: str,
    fecha: str | None = None,
    intentos: int = INTENTOS,
    perfil: Path = PERFIL,
    carpeta_dia: Path | None = None,
    _llamar=planificador.llamar_ollama,
) -> dict:
    """Mensaje de Antonio → dict con la respuesta validada del agente.

    NO aplica efectos (escribir eventos/perfil es del llamador via aplicar):
    así los tests y el servidor manejan el mismo dict. `carpeta_dia` (por
    defecto out/<fecha>) y `perfil` son inyectables para aislar los tests.
    """
    fecha = fecha or registro.fecha_de_plan()
    carpeta = carpeta_dia or (pipeline.SALIDA / fecha)
    rutinas = rutinas or {}
    base = construir_prompt(texto, carpeta, fecha, rutinas, perfil=perfil)
    errores, elegido = [], None
    for intento in range(1, max(intentos, 1) + 1):
        # El reintento SABE lo que pasó antes, igual que el planificador,
        # que lo hace con un `if` y no con un condicional inline: así el
        # turno limpio es exactamente `base` (el inline de antes construía
        # base + base — el prompt entero por duplicado).
        prompt = base
        if errores:
            prompt = base + (
                "\n\nCORRECCIÓN OBLIGATORIA: tu JSON anterior NO sirve por esto: "
                + "; ".join(errores)
                + ". Regenera el JSON completo corrigiendo SOLO esos puntos."
            )
        elegido = _elegir_respuesta(_llamar(prompt, modelo))
        if elegido is None:
            errores = ["la respuesta no lleva un JSON utilizable con la clave respuesta"]
            log.warning("Intento %d sin JSON utilizable", intento)
            continue
        errores = _validar(elegido)
        if not errores:
            resultado = _normalizar(elegido)
            log.info(
                "Respuesta del chat (%s): registrar=%r aprender=%r",
                resultado["respuesta"][:60], resultado["registrar"], resultado["aprender"],
            )
            return resultado
        log.warning(
            "Intento %d con %d problema(s) de validación: %s", intento, len(errores), "; ".join(errores)
        )
    raise RuntimeError("El chat no dio una respuesta usable tras los reintentos: " + "; ".join(errores))


def aplicar(resultado: dict, salida_fecha: Path, perfil: Path = PERFIL) -> list[str]:
    """Aplica las acciones de una respuesta validada; devuelve lo hecho.

    Fase de aprendizaje: solo registrar (diario del día) y aprender
    (perfil). Devuelve las etiquetas de lo aplicado (las pinta la web).
    """
    hechos = []
    if resultado.get("registrar"):
        registro.registrar_evento(salida_fecha, "chat_registro", {
            "texto": resultado["registrar"],
        })
        hechos.append("registrar")
    if resultado.get("aprender"):
        guardar_aprendizaje(resultado["aprender"], perfil)
        hechos.append("aprender")
    return hechos