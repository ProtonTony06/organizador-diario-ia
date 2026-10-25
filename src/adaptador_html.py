"""Adaptador de presentación: convierte el plan del día de Markdown a HTML.

El Markdown (plan.md) es la base de verdad; este módulo solo genera la vista
(plan.html por día y un index.html de historial). Mismo patrón que el
adaptador del resumen-diario-ia: rutas relativas, plantilla con
{{TITULO}}/{{CONTENIDO}}, y el primer '# ' eliminado del cuerpo.
"""

import argparse
import html
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import markdown

RAIZ = Path(__file__).resolve().parent.parent
PLANTILLA = RAIZ / "templates" / "plan.html"
NOMBRE_PLAN = "plan.html"
NOMBRE_INDICE = "index.html"

log = logging.getLogger("adaptador_html")

PATRON_TITULO = re.compile(r"^#\s+(.+)$", re.MULTILINE)
PATRON_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def montar_pagina(ruta_md: Path, plantilla: Path = PLANTILLA) -> str:
    """Rellena la plantilla con el título del primer '# ' y el cuerpo."""
    texto = ruta_md.read_text(encoding="utf-8")
    m = PATRON_TITULO.search(texto)
    titulo = m.group(1).strip() if m else ruta_md.stem
    cuerpo = markdown.markdown(
        PATRON_TITULO.sub("", texto, count=1).lstrip(),
        extensions=["extra", "sane_lists"],
    )
    return plantilla.read_text(encoding="utf-8").replace(
        "{{TITULO}}", html.escape(titulo)
    ).replace("{{CONTENIDO}}", cuerpo)


def md_a_html(ruta_md: Path) -> Path:
    """Escribe plan.html junto al .md recibido y devuelve su ruta."""
    ruta_html = ruta_md.with_name(NOMBRE_PLAN)
    ruta_html.write_text(montar_pagina(ruta_md), encoding="utf-8")
    log.info("Página del día: %s", ruta_html)
    return ruta_html


def generar_indice(raiz_out: Path) -> Path:
    """Index del historial: un enlace por carpeta de fecha, la más nueva arriba."""
    dias = sorted(carpeta.name for carpeta in raiz_out.iterdir() if PATRON_FECHA.fullmatch(carpeta.name))
    lineas = []
    for dia in reversed(dias):
        try:
            fecha_bonita = datetime.strptime(dia, "%Y-%m-%d").strftime("%d/%m/%Y (%A)").capitalize()
        except ValueError:
            fecha_bonita = dia
        lineas.append(f"- [{fecha_bonita}]({dia}/{NOMBRE_PLAN})")
    cuerpo = "\n".join(lineas) or "(aún no hay planes)"
    pagina = f"# Planes del organizador\n\n{cuerpo}\n"
    destino = raiz_out / NOMBRE_INDICE
    destino.write_text(montar_html_index(pagina), encoding="utf-8")
    log.info("Índice del historial: %s", destino)
    return destino


def montar_html_index(pagina_md: str) -> str:
    """Mismo esqueleto que las páginas, con el índice como contenido."""
    titulo = f"Planes — {datetime.now().strftime('%d/%m/%Y')}"
    cuerpo = markdown.markdown(pagina_md, extensions=["sane_lists"])
    return PLANTILLA.read_text(encoding="utf-8").replace(
        "{{TITULO}}", html.escape(titulo)
    ).replace("{{CONTENIDO}}", cuerpo)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Convierte un plan.md en HTML.")
    parser.add_argument("md", type=Path)
    rutas = parser.parse_args()
    try:
        md_a_html(rutas.md)
    except Exception as error:  # noqa: BLE001 — el fallo se reporta, no se oculta
        log.error("Fallo generando HTML: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())