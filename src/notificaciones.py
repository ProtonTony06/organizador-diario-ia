"""Módulo de notificaciones: avisa al móvil vía ntfy con el plan del día.

Mismo patrón que el resumen-diario-ia (copiado y adaptado): la URL del
topic vive en config/config.yaml (notificaciones.ntfy_url) y vacía =
deshabilitado. Nada de este módulo puede tumbar el pipeline.
"""

import logging
from pathlib import Path
from urllib import request

import yaml

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = RAIZ / "config" / "config.yaml"

log = logging.getLogger("notificaciones")


def _leer_ntfy_url(ruta: Path = CONFIG) -> str:
    """Lee notificaciones.ntfy_url de config.yaml (vacío = deshabilitado)."""
    try:
        with ruta.open(encoding="utf-8") as fichero:
            config = yaml.safe_load(fichero) or {}
        return (config.get("notificaciones") or {}).get("ntfy_url") or ""
    except Exception as error:  # noqa: BLE001 — la config no debe petar el pipeline
        log.warning("Config de notificaciones ilegible (%s): se deshabilitan", error)
        return ""


def _leer_config_clave(clave: str, ruta: Path = CONFIG) -> str:
    """Lee una clave de primer nivel de config.yaml (vacía si falta)."""
    try:
        with ruta.open(encoding="utf-8") as fichero:
            config = yaml.safe_load(fichero) or {}
        return config.get(clave) or ""
    except Exception as error:  # noqa: BLE001 — la config no debe petar el pipeline
        log.warning("Config ilegible (%s): web_base vacío", error)
        return ""


def url_del_dia(fecha: str) -> str:
    """URL absoluta de la página del día para el Click de ntfy (vacío si no hay)."""
    base = _leer_config_clave("web_base").rstrip("/")
    return f"{base}/{fecha}/plan.html" if base else ""


def avisar(titular: str, url_click: str) -> bool:
    """Envía el titular a ntfy con cabecera Click: url_click.

    Devuelve True si se envió; nunca lanza (un fallo de notificación no
    invalida el plan del día, que ya está publicado).
    """
    ntfy_url = _leer_ntfy_url(CONFIG)
    if not ntfy_url:
        log.info("Notificación deshabilitada (ntfy_url vacío en config.yaml)")
        return False
    peticion = request.Request(  # noqa: S310 — URL de la propia configuración
        ntfy_url, data=titular.encode("utf-8"), method="POST",
        headers={"Click": url_click, "Title": "Plan del día", "Tags": "calendar"},
    )
    try:
        with request.urlopen(peticion, timeout=10) as respuesta:  # noqa: S310
            respuesta.read()
        log.info("Notificación enviada a %s", ntfy_url)
        return True
    except Exception as error:  # noqa: BLE001 — avisar nunca debe petar el pipeline
        log.warning("No se pudo enviar la notificación: %s", error)
        return False