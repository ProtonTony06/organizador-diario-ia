"""Parsea el inbox de tareas (Markdown con checkboxes) a datos estructurados.

El inbox es la base de verdad de las tareas; este módulo es su única puerta.
Formato de línea (feedback del proyecto: plano, con checkboxes):

    - [ ] [dev] Corregir el bug del login (est: 45m)
    - [ ] [formacion] Sección 3 (est: 1h, antes: 2026-10-31)
    - [x] [hogar] Hecha                     ← se ignora en el plan

Cada tarea recibe un id estable (sha1 de categoría+texto, 6 hex): el plan
lo referenciará y la post-validación lo exigirá.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("entrada")

# Categorías válidas: la línea con una no reconocida se acepta igualmente
# (como "personal") pero se advierte — la validación del plan la contará.
CATEGORIAS = ("dev", "formacion", "hogar", "personal")

LINEA = re.compile(r"^[-*]\s+\[(?P<hecha>[ xX])\]\s*(?:\[(?P<cat>[a-zA-Z]+)\])?\s*(?P<texto>.+?)\s*$")
PARENTESIS = re.compile(r"^(?P<texto>.*?)\s*\((?P<meta>[^()]*[A-Za-z0-9])[^()]*\)\s*$")
EST = re.compile(r"^\s*est:\s*(?P<val>[^,]+)\s*", re.IGNORECASE)
ANTES = re.compile(r"^\s*antes:\s*(?P<val>\d{4}-\d{2}-\d{2})", re.IGNORECASE)


@dataclass
class Tarea:
    texto: str
    categoria: str = "personal"
    minutos: int | None = None       # estimación (50 min por defecto en el plan)
    antes: str = ""                   # fecha límite YYYY-MM-DD ("" si no tiene)
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


def parsear_linea(linea: str) -> Tarea | None:
    """Una línea del inbox → Tarea; None si no es tarea (o está hecha)."""
    m = LINEA.match(linea)
    if not m or m.group("hecha").lower() == "x":
        return None
    categoria = m.group("cat") or "personal"
    if categoria.lower() not in CATEGORIAS:
        log.warning(
            "Categoría desconocida %r (cuenta como personal): %s",
            categoria, m.group("texto"),
        )
        categoria = "personal"
    texto = m.group("texto")
    minutos, antes = None, ""
    p = PARENTESIS.match(texto)
    if p:
        minutos, antes = _dividir_meta(p.group("meta"))
        if minutos is not None or antes:
            texto = p.group("texto")
    return Tarea(texto=texto.strip(), categoria=categoria, minutos=minutos, antes=antes)


def leer(ruta) -> list[Tarea]:
    """Lee el fichero del inbox y devuelve las tareas pendientes."""
    with open(ruta, encoding="utf-8") as fichero:
        lineas = fichero.read().splitlines()
    pendientes = []
    for linea in lineas:
        tarea = parsear_linea(linea)
        if tarea:
            pendientes.append(tarea)
    return pendientes


def a_prompt(tareas: list[Tarea]) -> str:
    """Lista numerada con ids para el prompt del modelo (verbatims de fuente)."""
    return "\n".join(
        f"- {t.id} [{t.categoria}] {t.texto}"
        + (f" ({t.minutos}m estimados" if t.minutos else "")
        + ("; fecha límite: " + t.antes if t.antes else "")
        + (")" if t.minutos else "")
        for t in tareas
    )