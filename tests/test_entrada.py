"""Tests del parser del inbox (entrada.py): formatos, estimaciones y ids."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from entrada import CATEGORIAS, parsear_linea  # noqa: E402


class TestParsearLinea(unittest.TestCase):
    def test_tarea_minima(self):
        tarea = parsear_linea("- [ ] Reservar cita del dentista")
        self.assertEqual(tarea.categoria, "personal")
        self.assertIsNone(tarea.minutos)
        self.assertEqual(tarea.texto, "Reservar cita del dentista")

    def test_tarea_completa(self):
        tarea = parsear_linea("- [ ] [formacion] Sección 3 (est: 1h, antes: 2026-10-31)")
        self.assertEqual(tarea.categoria, "formacion")
        self.assertEqual(tarea.minutos, 60)
        self.assertEqual(tarea.antes, "2026-10-31")
        self.assertEqual(tarea.texto, "Sección 3")

    def test_estimaciones_variadas(self):
        for linea, esperado in (
            ("- [ ] [dev] A (est: 45m)", 45),
            ("- [ ] [dev] B (est: 1h30m)", 90),
            ("- [ ] [dev] C (est: 90 min)", 90),
            ("- [ ] [dev] D (est: 2 h)", 120),
        ):
            with self.subTest(linea=linea):
                self.assertEqual(parsear_linea(linea).minutos, esperado)

    def test_hecha_se_ignora(self):
        self.assertIsNone(parsear_linea("- [x] [dev] Hecha (est: 1h)"))

    def test_checkbox_sin_texto_es_tarea_fantasma(self):
        # M6 (2026-10-25): un checkbox cuyo texto SOLO era meta generaba una
        # Tarea con texto vacío (id estable de la nada, "tarea fantasma")
        self.assertIsNone(parsear_linea("- [ ] (est: 45m)"))
        self.assertIsNone(parsear_linea("- [ ] [dev] (est: 45m, antes: 2026-10-31)"))

    def test_id_estable_por_texto(self):
        primera = parsear_linea("- [ ] [dev] A")
        # misma tarea tras líneas ajenas: el id solo depende de categoría+texto
        segunda = parsear_linea("- [ ] [dev] A")
        self.assertEqual(primera.id, segunda.id)

    def test_ids_distintos_con_distinta_categoria(self):
        self.assertNotEqual(
            parsear_linea("- [ ] [dev] A").id,
            parsear_linea("- [ ] [hogar] A").id,
        )

    def test_categorias_validas(self):
        self.assertIn("dev", CATEGORIAS)


if __name__ == "__main__":
    unittest.main()