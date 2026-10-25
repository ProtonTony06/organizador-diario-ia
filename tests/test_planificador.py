"""Tests del planificador: utilidades de tiempos, validación y plan mecánico.

Solo funciones puras: NO se llama a llamar_ollama ni a planificar (nada de
gastar modelo en tests). La validación usa rutinas de ejemplo propias y el
plan mecánico se verifica contra config/rutinas.yaml real.
"""

import sys
import unittest
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from entrada import Tarea  # noqa: E402
import planificador  # noqa: E402


def rutinas_ejemplo():
    """Rutinas pequeñas y deterministas para _validar (1 ancla, 1 capacidad)."""
    return {
        "ventana": {"inicio": "07:30", "fin": "01:00"},
        "anclas": [{"momento": "14:30", "duracion": 90, "nombre": "Comida"}],
        "capacidades": {"dev": 150},
    }


def bloque(inicio: str, fin: str, tarea: str | None = None, tipo: str = "foco") -> dict:
    return {"inicio": inicio, "fin": fin, "tipo": tipo, "tarea": tarea}


class TestMinutos(unittest.TestCase):
    def test_horas_normales(self):
        self.assertEqual(planificador._minutos("07:30"), 450)
        self.assertEqual(planificador._minutos("23:59"), 1439)

    def test_cruce_de_medianoche(self):
        # horas < 6 cuentan como día siguiente (ventana que pasa de medianoche)
        self.assertEqual(planificador._minutos("01:00"), 1500)

    def test_formato_ilegible(self):
        self.assertIsNone(planificador._minutos("sin hora"))
        self.assertIsNone(planificador._minutos(""))

    def test_hhmm_ida_y_vuelta(self):
        for hora, esperado in (("07:30", 450), ("01:00", 1500), ("23:59", 1439)):
            with self.subTest(hora=hora):
                minutos = planificador._minutos(hora)
                self.assertEqual(minutos, esperado)
                self.assertEqual(planificador._hhmm(minutos), hora)


class TestAnclasMingan(unittest.TestCase):
    def test_dos_anclas_ordenadas(self):
        rutinas = {
            "anclas": [
                {"momento": "23:30", "duracion": 40, "nombre": "Cena"},
                {"momento": "08:00", "duracion": 30, "nombre": "Desayuno"},
            ],
        }
        self.assertEqual(
            planificador._anclas_mingan(rutinas),
            [(480, 510, "Desayuno"), (1410, 1450, "Cena")],
        )


class TestValidar(unittest.TestCase):
    def setUp(self):
        self.rutinas = rutinas_ejemplo()
        self.t1 = Tarea(texto="Refactor del parser", categoria="dev", minutos=120, id="dev-1")
        self.t2 = Tarea(texto="Ligar pagos", categoria="dev", minutos=30, id="dev-2")
        self.tareas = [self.t1, self.t2]

    def test_plan_valido(self):
        respuesta = {
            "plan": [
                bloque("07:30", "09:30", "dev-1"),
                bloque("12:00", "12:30", None, tipo="personal"),
                bloque("00:30", "01:00", "dev-2"),  # cruza medianoche: sí es de la ventana
            ],
            "no_caben": [],
        }
        self.assertEqual(planificador._validar(respuesta, self.tareas, self.rutinas), [])

    def test_id_desconocido(self):
        # ARRREGLO 2026-10-25: antes _validar lanzaba KeyError por el bucle de
        # capacidades con un tid no validado (bug cazado por este test cuando
        # estaba marcado @unittest.expectedFailure). Ahora debe DEVOLVER el
        # error, nunca reventar: un error de validación tiene que poder
        # alimentar el reintento del modelo y el plan mecánico de rescate.
        # (no_caben completado para la regla de "todo pendiente clasificado";
        # lo que este test verifica sigue siendo el id desconocido.)
        respuesta = {
            "plan": [bloque("12:00", "13:00", "fantasma")],
            "no_caben": ["dev-1", "dev-2"],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("fantasma", errores[0])

    def test_bloques_solapados_entre_si(self):
        respuesta = {
            "plan": [bloque("08:00", "09:00", "dev-1"), bloque("08:30", "10:00", "dev-2")],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("solapados", errores[0])

    def test_bloque_pisa_ancla(self):
        # la ancla Comida va de 14:30 a 16:00
        # (no_caben con dev-2: la regla "todo pendiente clasificado", 2026-10-25)
        respuesta = {"plan": [bloque("14:00", "15:00", "dev-1")], "no_caben": ["dev-2"]}
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("Comida", errores[0])

    def test_fuera_de_ventana(self):
        # antes del 07:30 (con dev-2 en "no_caben" por la regla de clasificación)
        respuesta = {"plan": [bloque("06:00", "07:00", "dev-1")], "no_caben": ["dev-2"]}
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("fuera de la ventana", errores[0])

    def test_cruce_de_medianoche_es_valido(self):
        # 00:30 es de la ventana 07:30–01:00 (el día planificado sigue)
        respuesta = {"plan": [bloque("00:30", "01:00", "dev-2")], "no_caben": ["dev-1"]}
        self.assertEqual(planificador._validar(respuesta, self.tareas, self.rutinas), [])

    def test_capacidad_dev_superada(self):
        # 120 + 90 = 210m de foco dev con capacidad de 150m
        respuesta = {
            "plan": [bloque("07:30", "09:30", "dev-1"), bloque("12:00", "13:30", "dev-2")],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("superan la capacidad", errores[0])

    def test_no_caben_con_id_desconocido(self):
        # (dev-2 también en "no_caben": la regla "todo pendiente clasificado")
        respuesta = {"plan": [bloque("07:30", "09:30", "dev-1")], "no_caben": ["fantasma", "dev-2"]}
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("\"no_caben\"", errores[0])

    def test_respuesta_sin_clave_plan(self):
        errores = planificador._validar({"comentario": "sin plan"}, self.tareas, self.rutinas)
        self.assertEqual(errores, ["\"plan\" no es una lista con bloques"])

    # Reglas de coherencia y cobertura (hallazgo del test real end-to-end,
    # 2026-10-25: el modelo puso una tarea pendiente como "personal" con solo
    # el título y se colaba → tareas duplicadas en la web).

    def test_bloque_foco_sin_tarea(self):
        respuesta = {
            "plan": [
                bloque("08:00", "09:00", "dev-1"),
                bloque("09:00", "09:30", None, tipo="foco"),  # foco hueco
            ],
            "no_caben": ["dev-2"],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("sin tarea", errores[0])

    def test_bloque_personal_no_lleva_tarea(self):
        respuesta = {
            "plan": [bloque("08:00", "09:00", "dev-1", tipo="personal")],
            "no_caben": ["dev-2"],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("no puede llevar tarea", errores[0])
        self.assertIn("personal", errores[0])

    def test_pendiente_sin_bloque_ni_no_caben(self):
        # dev-2 no aparece en ningún sitio: la regla 2 lo señala para que el
        # modelo lo enlace con id (planificarlo por título NO vale)
        respuesta = {"plan": [bloque("08:00", "09:00", "dev-1")], "no_caben": []}
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(
            [e for e in errores if f"la tarea dev-2 debe" in e],
            ['la tarea dev-2 debe ir en un bloque foco o en "no_caben"'],
        )

    def test_tarea_en_varios_bloques(self):
        # I5: el duplicado se reporta y ADEMÁS suma minutos (60+60=120 ≤ 150,
        # así que NO sale el error de capacidad: solo el del duplicado)
        respuesta = {
            "plan": [bloque("08:00", "09:00", "dev-1"), bloque("09:00", "10:00", "dev-1")],
            "no_caben": ["dev-2"],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertEqual(len(errores), 1)
        self.assertIn("aparece en más de un bloque", errores[0])
        self.assertIn("dev-1", errores[0])

    def test_tarea_duplicada_suma_para_la_capacidad(self):
        # I5 (la otra cara): duplicada + minutos sumados sí superan capacidad
        # 120 + 30 = 150 no; 120+60=180 > 150 → ambos errores
        respuesta = {
            "plan": [bloque("08:00", "10:00", "dev-1"), bloque("10:30", "11:30", "dev-1")],
            "no_caben": ["dev-2"],
        }
        # ojo: 10:30 pisa el Desayuno (10:00–10:30 no, empieza a las 10:30 ok)
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        self.assertIn("aparece en más de un bloque", " ".join(errores))
        self.assertIn("superan la capacidad", " ".join(errores))
        self.assertEqual(len(errores), 2)

    def test_solape_anidado_no_oculta_el_siguiente(self):
        # I7: el bloque anidado (08:30–08:45) acababa ANTES que el que lo
        # contiene (08:00–10:00); con la reasignación directa el tercero
        # (09:30–10:30) ya no se comparaba contra el fin real y el solape
        # A–C pasaba desapercibido.
        respuesta = {
            "plan": [
                bloque("08:00", "10:00", "dev-1"),
                bloque("08:30", "08:45", None, tipo="personal"),
                bloque("09:30", "09:45", "dev-2"),
            ],
            "no_caben": [],
        }
        errores = planificador._validar(respuesta, self.tareas, self.rutinas)
        solapes = [e for e in errores if "solapados" in e]
        self.assertEqual(len(solapes), 2)  # A–B y A–C; antiguamente se perdía A–C


class TestPlanMecanico(unittest.TestCase):
    def setUp(self):
        with planificador.RUTINAS.open(encoding="utf-8") as fichero:
            self.rutinas = yaml.safe_load(fichero)

    def test_plan_dentro_de_ventana_y_sin_pisar_anclas(self):
        tareas = [
            Tarea(texto="Refactor del parser", categoria="dev", minutos=60, id="dev-a"),
            Tarea(texto="Revisar PR", categoria="dev", minutos=45, id="dev-b"),
            Tarea(texto="Leer sección 3", categoria="formacion", minutos=30, id="for-a"),
        ]
        resultado = planificador.plan_mecanico(tareas, self.rutinas)

        ventana_inicio = planificador._minutos(str(self.rutinas["ventana"]["inicio"]))
        ventana_fin = planificador._minutos(str(self.rutinas["ventana"]["fin"]))
        if ventana_fin <= ventana_inicio:
            ventana_fin += 24 * 60
        anclas = planificador._anclas_mingan(self.rutinas)

        puestos = []
        for b in resultado["plan"]:
            inicio = planificador._minutos(b["inicio"])
            fin = planificador._minutos(b["fin"])
            self.assertGreaterEqual(inicio, ventana_inicio, b)
            self.assertLessEqual(fin, ventana_fin, b)
            for a_inicio, a_fin, nombre in anclas:
                self.assertFalse(inicio < a_fin and a_inicio < fin, f"{b} pisa {nombre}")
            puestos.append((inicio, fin))

        puestos.sort()
        for previa, siguiente in zip(puestos, puestos[1:]):
            self.assertLessEqual(previa[1], siguiente[0], f"{previa} solapa {siguiente}")

        self.assertEqual(resultado["fallback"], True)
        self.assertEqual(resultado["valido"], True)
        self.assertEqual(resultado["no_caben"], [])
        self.assertEqual([b["tarea"] for b in resultado["plan"]], ["dev-a", "dev-b", "for-a"])

    def test_tarea_mayor_que_el_dia_va_a_no_caben(self):
        larga = Tarea(texto="Mudanza completa", categoria="personal", minutos=2000, id="larga")
        resultado = planificador.plan_mecanico([larga], self.rutinas)
        self.assertEqual(resultado["no_caben"], ["larga"])
        self.assertEqual(resultado["plan"], [])
        self.assertEqual(resultado["fallback"], True)
        self.assertEqual(resultado["valido"], True)


if __name__ == "__main__":
    unittest.main()