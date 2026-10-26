# organizador-diario-ia

Organizador del día de Antonio: cada mañana genera un **plan del día** a partir
del inbox de tareas (MD), las rutinas fijas (ventana, comidas, capacidades) y
las tareas pendientes, lo publica como página web y avisa por ntfy.

Hermano de `resumen-diario-ia`: mismo esqueleto (CT Proxmox, timer matutino,
Ollama cloud, MD en NAS, ntfy), otra lógica dentro.

Fases: **F0** plan matutino + inbox ✅ · **F1** web de cierre con timestamps y
re-plan del resto del día (PWA) ✅ · **F2** chat de contexto — FASE DE
APRENDIZAJE: 1–2 semanas donde el agente solo escucha (registra hechos en el
diario del día y aprende rutina estable en `data/perfil.md`); NO cierra
tareas ni re-planifica desde el chat. · F3 chat conversacional completo.

## Uso

```bash
./.venv/bin/python src/pipeline.py            # plan de hoy, con notificación
./.venv/bin/python src/pipeline.py --no-notificar
./.venv/bin/python src/pipeline.py --listar   # ver el inbox parseado
./.venv/bin/python src/servidor.py            # web + API + chat en :8010
```

## Estructura

```
config/config.yaml    modelo Ollama, web_base, topic ntfy
config/rutinas.yaml   ventana del día, anclas fijas, capacidades por categoría
data/inbox.md         la base de verdad de las tareas (checkboxes, categorías)
data/perfil.md        rutina aprendida por el chat (LÍNEAS - (fecha) patrón)
src/entrada.py        parsea/normaliza el inbox MD → tareas estructuradas
src/planificador.py   situación + inbox → Ollama → JSON validado del plan
src/chat.py           F2: contexto + respuestas/acciones del chat validadas
src/servidor.py       web de cierre (F1) + API + chat (F2) + PWA en :8010
src/pipeline.py       orquestador: inbox → plan → MD + HTML → notificar
src/adaptador_html.py Markdown → HTML (plantilla) + índice del histórico
src/notificaciones.py aviso ntfy con Click (never-throw, como en el resumen)
templates/web/        index.html (Plan · Chat), manifest, icono, sw.js
templates/plan.html   esqueleto de la página del día
out/<fecha>/          plan.md (base de verdad) + plan.html + eventos.jsonl diario
                      + chat.jsonl (conversación del día)
```

## Formato del inbox

```markdown
- [ ] [dev] Corregir el bug del login (est: 45m)
- [ ] [formacion] Terapia 3 de MasterD (est: 1h, antes: 2026-10-31)
- [ ] [hogar] Comprar aceite (est: 15m)
- [x] [dev] Tarea ya hecha        ← se ignora en el plan
```

Categorías del planificador: `dev`, `formacion`, `hogar`, `personal`.
(`est:` en minutos/horas; `antes:` fecha límite opcional).