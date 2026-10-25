"""Tests del Markdown del plan: normalización de ids en bloques_del_dia."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import plan_md  # noqa: E402


class TestBloquesDelDia(unittest.TestCase):
    RUTINAS = {"anclas": [{"momento": "10:00", "duracion": 30, "nombre": "Desayuno"}]}

    def test_id_null_no_es_tarea(self):
        # I6 (2026-10-25): "null"/"None" como STRING del JSON del modelo
        # pasaban al plan.json con la cadena "null" y la web buscaba una
        # tarea con id "null". Normalizado igual que _validar: "".
        for basura in (None, "null", "None", " NULL ", ""):
            with self.subTest(basura=basura):
                resultado = {"plan": [{
                    "inicio": "08:00", "fin": "09:00", "tipo": "personal",
                    "tarea": basura, "titulo": "Paseo",
                }]}
                bloques = plan_md.bloques_del_dia(resultado, self.RUTINAS)
                self.assertEqual(bloques[1]["tarea"], "")

    def test_ancla_lleva_tarea_vacia(self):
        bloques = plan_md.bloques_del_dia({"plan": []}, self.RUTINAS)
        self.assertEqual(bloques[0]["tarea"], "")
        self.assertEqual(bloques[0]["tipo"], "ancla")


if __name__ == "__main__":
    unittest.main()