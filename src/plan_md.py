"""Convierte el JSON validado del plan + rutinas en el Markdown del día.

Base de verdad del día: aquí las ANCLAS se insertan determinísticamente
( el modelo nunca las emite ) y se mezclan con los bloques del modelo
ordenados por hora. El HTML es derivado de este Markdown (adaptador).
"""

import logging
from datetime import datetime

import yaml
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUTINAS = RAIZ / "config" / "rutinas.yaml"

log = logging.getLogger("plan_md")

TITULOS_TIPO = {"foco": "Foco", "personal": "Personal", "descanso": "Descanso"}
DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _minutos(hora: str) -> int:
    """'07:30' → minutos (las horas < 6 cuentan tras la medianoche)."""
    parte_hora, _, parte_minuto = hora.strip().partition(":")
    valor = int(parte_hora) * 60 + int(parte_minuto or 0)
    if int(parte_hora) < 6:
        valor += 24 * 60
    return valor


def _hhmm(minutos: int) -> str:
    minutos %= 24 * 60
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


def bloques_del_dia(resultado: dict, rutinas: dict) -> list[dict]:
    """Anclas fijas + bloques del modelo, unidos y ordenados por hora."""
    bloques = []
    for ancla in rutinas.get("anclas") or []:
        inicio = _minutos(str(ancla["momento"]))
        bloques.append({
            "inicio": inicio, "fin": inicio + int(ancla.get("duracion", 30)),
            "tipo": "ancla", "titulo": ancla["nombre"], "tarea": "", "nota": "",
        })
    for bloque in resultado.get("plan") or []:
        inicio = _minutos(str(bloque["inicio"]))
        bloques.append({
            "inicio": inicio, "fin": _minutos(str(bloque["fin"])),
            "tipo": str(bloque.get("tipo") or "foco"),
            "titulo": str(bloque.get("titulo") or ""),
            "tarea": str(bloque.get("tarea") or ""),
            "nota": str(bloque.get("nota") or ""),
        })
    return sorted(bloques, key=lambda b: (b["inicio"], b["fin"]))


def a_markdown(resultado: dict, tareas: list, fecha: str, rutinas: dict) -> str:
    """Markdown del plan del día: timeline + tareas sin hueco + comentario."""
    por_id = {t.id: t for t in tareas}
    dia = datetime.strptime(fecha, "%Y-%m-%d")
    dia_semana = DIAS_SEMANA[dia.weekday()].capitalize()
    partes = [f"# Plan del día — {dia_semana} {dia.strftime('%d/%m')}", ""]

    bloques = bloques_del_dia(resultado, rutinas)
    if bloques:
        for bloque in bloques:
            rango = f"**{_hhmm(bloque['inicio'])}–{_hhmm(bloque['fin'])}**"
            etiqueta = TITULOS_TIPO.get(bloque["tipo"], bloque["tipo"] or "Bloque")
            tid = bloque.get("tarea") or ""
            tarea = por_id.get(tid) if tid else None
            if tarea is not None:
                titulo = tarea.texto
                categoria = tarea.categoria
                minutos = f" ({bloque['fin'] - bloque['inicio']}m)"
            else:
                titulo = bloque["titulo"] or "—"
                categoria = "ancla" if bloque["tipo"] == "ancla" else bloque["tipo"]
                minutos = ""
            nota = f" — *{bloque['nota']}*" if bloque.get("nota") else ""
            partes.append(f"- {rango} · {etiqueta} · {titulo}{minutos}{nota}")
        partes.append("")
    else:
        partes.append("(sin bloques planificados hoy)\n")

    caben = sum(1 for t in tareas if any(b.get("tarea") == t.id for b in resultado.get("plan") or []))
    partes.append(f"*Tareas planificadas hoy: {caben} de {len(tareas)} del inbox.*")
    no_caben = [por_id[i] for i in (resultado.get("no_caben") or []) if i in por_id]
    if no_caben:
        partes.append("")
        partes.append("## Sin hueco hoy")
        partes.append("")
        partes.extend(f"- [{t.categoria}] {t.texto}" for t in no_caben)
    comentario = str(resultado.get("comentario") or "").strip()
    if comentario:
        partes.append("")
        partes.append(f"> {comentario}")
    return "\n".join(partes) + "\n"


def leer_rutinas(ruta: Path = RUTINAS) -> dict:
    with ruta.open(encoding="utf-8") as fichero:
        return yaml.safe_load(fichero) or {}