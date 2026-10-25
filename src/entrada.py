"""Parsea el inbox de tareas (Markdown con checkboxes) a datos estructurados.

El inbox es la base de verdad de las tareas; este módulo es su única puerta.
Formato de línea (feedback del proyecto: plano, con checkboxes):

    - [ ] [dev] Corregir el bug del login (est: 45m)
    - [ ] [formacion] Sección 3 (est: 1h, antes: 2026-10-31)
    - [x] [hogar] Comprar pan (est: 15m) (hecha: 2026-10-25 18:03)

Cada tarea recibe un id estable (sha1 de categoría+texto, 6 hex): el plan
lo referencia, la post-validación lo exige y la web lo usa para cerrar.
Una tarea CERRADA conserva el mismo texto y meta, por lo que el id no
cambia al cerrarse — solo pasa a [x] y se le añade la marca de hora AL
FINAL de la línea (el parser la elimina antes de computar el id).
"""

import hashlib
import logging
import re
from dataclasses import dataclass

log = logging.getLogger("entrada")

# Categorías válidas: la línea con una no reconocida se acepta igualmente
# (como "personal") pero se advierte.
CATEGORIAS = ("dev", "formacion", "hogar", "personal")

LINEA = re.compile(r"^[-*]\s+\[(?P<hecha>[ xX])\]\s*(?:\[(?P<cat>[a-zA-Z]+)\])?\s*(?P<texto>.+?)\s*$")
PARENTESIS = re.compile(r"^(?P<texto>.*?)\s*\((?P<meta>[^()]*[A-Za-z0-9])[^()]*\)\s*$")
EST = re.compile(r"^\s*est:\s*(?P<val>[^,]+)\s*", re.IGNORECASE)
ANTES = re.compile(r"^\s*antes:\s*(?P<val>\d{4}-\d{2}-\d{2})", re.IGNORECASE)
HECHA = re.compile(r"\s*\(hecha:\s*(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*\)")


@dataclass
class Tarea:
    texto: str
    categoria: str = "personal"
    minutos: int | None = None       # estimación (el plan dura lo que el bloque)
    antes: str = ""                   # fecha límite YYYY-MM-DD ("" si no tiene)
    hecha: bool = False               # True si la línea va con [x]
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = hashlib.sha1(
                f"{self.categoria}|{self.texto}".encode("utf-8")
            ).hexdigest()[:6]


def _parsear_estimacion(valor: str) -> int | None:
    """'45m' | '1h' | '1h30m' | '90' → minutos; None si no se entiende."""
    valor = valor.strip().lower().replace("min", "m").replace("hora", "h").replace("hours", "h")
    horas = minutos = 0.0
    for numero, unidad in re.findall(r"(\d+(?:[.,]\d+)?)\s*([mh]?)", valor):
        cantidad = float(numero.replace(",", "."))
        if unidad == "h":
            horas += cantidad
        else:
            minutos += cantidad
    total = int(horas * 60 + minutos)
    return total or None


def _dividir_meta(meta: str) -> tuple[int | None, str]:
    """Saca 'est:' y 'antes:' de dentro del paréntesis de una línea."""
    minutos, antes = None, ""
    for trozo in meta.split(","):
        m = EST.match(trozo)
        if m:
            minutos = _parsear_estimacion(m.group("val"))
            continue
        m = ANTES.match(trozo)
        if m:
            antes = m.group("val")
    return minutos, antes


def parsear_linea(linea: str, incluir_hechas: bool = False) -> Tarea | None:
    """Una línea del inbox → Tarea; None si no es tarea.

    De serie devuelve solo las PENDIENTES (los planes ignoran lo cerrado);
    con incluir_hechas=True también las cerradas (con hecha=True), y en ese
    caso se elimina la marca '(hecha: ...)' ANTES de computar el id, para
    que una tarea cerrada conserve el id que tuvo de pendiente.
    """
    m = LINEA.match(linea)
    if not m:
        return None
    hecha = m.group("hecha").lower() == "x"
    if hecha and not incluir_hechas:
        return None
    texto = HECHA.sub("", m.group("texto"))
    categoria = m.group("cat") or "personal"
    if categoria not in CATEGORIAS:
        log.warning("Categoría desconocida %r (cuenta como personal): %s", categoria, texto)
        categoria = "personal"
    minutos, antes = None, ""
    p = PARENTESIS.match(texto)
    if p:
        minutos, antes = _dividir_meta(p.group("meta"))
        if minutos is not None or antes:
            texto = p.group("texto")
    texto = texto.strip()
    if not texto:
        # M6 (2026-10-25): un checkbox cuyo texto SOLO era meta — p. ej.
        # `- [ ] (est: 45m)` — generaba una Tarea con texto vacío (el id,
        # un sha1 de "personal|": id estable de una tarea fantasma). Una
        # línea así no es una tarea: se descarta con un aviso.
        log.warning("Línea de checkbox sin texto: se ignora (tarea fantasma): %r", linea)
        return None
    return Tarea(texto=texto, categoria=categoria, minutos=minutos, antes=antes, hecha=hecha)


def leer(ruta) -> list[Tarea]:
    """Lee el fichero del inbox y devuelve las tareas pendientes."""
    return [t for t in _recorrer(ruta) if not t.hecha]


def leer_todas(ruta) -> list[Tarea]:
    """Como leer(), pero incluye también las tareas cerradas (hecha=True)."""
    return list(_recorrer(ruta))


def _recorrer(ruta):
    with open(ruta, encoding="utf-8") as fichero:
        lineas = fichero.read().splitlines()
    for linea in lineas:
        if linea.strip() and not linea.lstrip().startswith("#"):
            tarea = parsear_linea(linea, incluir_hechas=True)
            if tarea is not None:
                yield tarea


def a_prompt(tareas: list[Tarea]) -> str:
    """Lista con ids para el prompt del modelo (los verbatims de fuente)."""
    lineas = []
    for t in tareas:
        linea = f"- {t.id} [{t.categoria}] {t.texto}"
        if t.minutos:
            linea += f" (~{t.minutos}m)"
        if t.antes:
            linea += f" (antes: {t.antes})"
        lineas.append(linea)
    return "\n".join(lineas)