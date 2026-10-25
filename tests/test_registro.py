"""Tests del registro del día: cierre con timestamp estable en el id + eventos."""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from entrada import leer, leer_todas, parsear_linea  # noqa: E402
from registro import cerrar_en_inbox, fecha_de_plan, leer_eventos, registrar_evento  # noqa: E402


INBOX = """# Inbox

- [ ] [dev] Corregir el bug del login (est: 45m)
- [ ] [formacion] Sección 3 de MasterD (est: 1h, antes: 2026-10-31)
- [x] [hogar] Comprar pan (est: 15m) (hecha: 2026-10-24 19:00)
"""


class TestCerrar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inbox = Path(self.tmp) / "inbox.md"
        self.inbox.write_text(INBOX, encoding="utf-8")

    def test_cerrar_mantiene_id(self):
        id_login = parsear_linea("- [ ] [dev] Corregir el bug del login (est: 45m)").id
        self.assertTrue(cerrar_en_inbox(self.inbox, id_login))
        todas = {t.id: t for t in leer_todas(self.inbox)}
        self.assertIn(id_login, todas)           # el id no cambia
        self.assertTrue(todas[id_login].hecha)
        self.assertEqual(len(leer(self.inbox)), 1)  # solo queda la formación

    def test_cerrar_preserva_meta(self):
        id_seccion = parsear_linea("- [ ] [formacion] Sección 3 de MasterD (est: 1h, antes: 2026-10-31)").id
        cerrar_en_inbox(self.inbox, id_seccion)
        cerrada = [t for t in leer_todas(self.inbox) if t.id == id_seccion][0]
        # el est y el "antes" siguen donde estaban (en el fichero)
        self.assertIn("(est: 1h", [l for l in self.inbox.read_text().splitlines() if id_seccion and "Sección 3" in l][0])

    def test_cierre_idempotente_no_duplica(self):
        id_login = parsear_linea("- [ ] [dev] Corregir el bug del login (est: 45m)").id
        cerrar_en_inbox(self.inbox, id_login)
        segundo = cerrar_en_inbox(self.inbox, id_login)
        self.assertFalse(segundo)  # ya cerrada: no hay línea pendiente
        hechas = [l for l in self.inbox.read_text().splitlines() if "(hecha:" in l]
        self.assertEqual(len(hechas), 2)  # la pre-cerrada del ejemplo + esta

    def test_id_invalido_no_falla_pero_no_toca(self):
        self.assertFalse(cerrar_en_inbox(self.inbox, "zzzzzz"))

    def test_cerrar_cierra_checkbox_de_asterisco(self):
        # I8 (2026-10-25): el replace de texto fijo dejaba abierta una
        # línea marcada con `*` en vez de `-`; la tarea quedaba pendiente.
        self.inbox.write_text(
            INBOX + "* [ ] [hogar] Regar plantas (est: 10m)\n", encoding="utf-8",
        )
        id_r = parsear_linea("* [ ] [hogar] Regar plantas (est: 10m)").id
        self.assertTrue(cerrar_en_inbox(self.inbox, id_r))
        cerrada = next(t for t in leer_todas(self.inbox) if t.id == id_r)
        self.assertTrue(cerrada.hecha)
        self.assertIn("(hecha:", [
            l for l in self.inbox.read_text().splitlines() if "Regar plantas" in l
        ][0])
        self.assertEqual(len(leer(self.inbox)), 2)  # pendientes: login y formación, ya no Regar

    def test_eventos_redondos(self):
        carpeta = Path(self.tmp) / "out" / "2026-10-25"
        registrar_evento(carpeta, "cierre", {"id_tarea": "abc123"})
        registrar_evento(carpeta, "re-plan_generado", {"bloques": 3})
        eventos = leer_eventos(carpeta)
        self.assertEqual([e["tipo"] for e in eventos], ["cierre", "re-plan_generado"])


class TestFechaDePlan(unittest.TestCase):
    def test_fecha_local_del_dia(self):
        # día normal (naive = local): la fecha tal cual
        self.assertEqual(fecha_de_plan(datetime(2026, 10, 26, 14, 30)), "2026-10-26")

    def test_tras_medianoche_sigue_el_dia_anterior(self):
        # B1: entre 00:00 y 05:59 pertenece AL DÍA QUE EMPEZÓ A LAS 07:30
        for hora, minuto in ((0, 45), (1, 30), (5, 59)):
            with self.subTest(hora=hora, minuto=minuto):
                self.assertEqual(fecha_de_plan(datetime(2026, 10, 26, hora, minuto)), "2026-10-25")

    def test_a_las_seis_ya_es_dia_nuevo(self):
        self.assertEqual(fecha_de_plan(datetime(2026, 10, 26, 6, 0)), "2026-10-26")


if __name__ == "__main__":
    unittest.main()