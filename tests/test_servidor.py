"""Tests HTTP del servidor: los endpoints /api/chat, sin ollama.

TestClient con chat.responder falsificada (el modelo NO se llama nunca) y
los ficheros del día redirigidos a un tmpdir (nunca a out/ ni a data/ del
repo: el perfil lo intercepta guardar_aprendizaje). Se fija aquí:
- el 409 en turno en marcha (un turno cada vez),
- que el estado NUNCA queda colgado en "pensando" ni siquiera si el
  arranque del turno o el propio mensaje de error fallan (el 409 eterno
  dejaría el chat acéfalo hasta reinicio),
- la forma EXACTA del mensaje del agente y de sus acciones
  ([{"hecho": "registrar"/"aprender", "texto": ...}]) de la que pinta la web.
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chat  # noqa: E402
import pipeline  # noqa: E402
import planificador  # noqa: E402
import servidor  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class TestApiChat(unittest.TestCase):
    """POST/GET /api/chat: el turno entero con el modelo falsificado."""

    def setUp(self):
        # out/ de verdad → tmpdir (los .jsonl de chat/eventos caen allí).
        self.raiz_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.raiz_tmp.cleanup)
        salida_original = pipeline.SALIDA
        pipeline.SALIDA = Path(self.raiz_tmp.name)
        self.addCleanup(setattr, pipeline, "SALIDA", salida_original)
        self.carpeta = servidor._carpeta_dia(servidor._fecha_de_hoy())
        # El aprendizaje va al perfil del repo: en los tests basta apuntarlo.
        self.aprendido = []
        guardar_original = chat.guardar_aprendizaje
        chat.guardar_aprendizaje = (lambda texto, *a, **k:
                                    (self.aprendido.append(texto) or True))
        self.addCleanup(setattr, chat, "guardar_aprendizaje", guardar_original)
        # El estado del chat es global del módulo: limpio en setUp y al salir
        # (un test no puede heredar el turno "pensando" del anterior).
        self.reponer_estado()
        self.addCleanup(self.reponer_estado)

    def reponer_estado(self):
        """Deja ESTADO_CHAT idle y sin error."""
        with servidor.CANDADO_CHAT:
            servidor.ESTADO_CHAT["estado"] = "idle"
            servidor.ESTADO_CHAT["error"] = None

    def falsificar_responder(self, resultado=None, excepcion=None, espera=None):
        """chat.responder falsificado; devuelve las llamadas que reciba."""
        original = chat.responder
        llamadas = []

        def falso(texto, rutinas, modelo, fecha=None, intentos=None, **k):
            llamadas.append({
                "texto": texto, "rutinas": rutinas, "modelo": modelo,
                "fecha": fecha, "intentos": intentos,
            })
            if excepcion is not None:
                raise excepcion
            if espera is not None:  # el turno "piensa" hasta que se avisa
                espera.wait(timeout=10)
            return resultado

        chat.responder = falso

        def restaurar():
            chat.responder = original
        self.addCleanup(restaurar)
        return llamadas

    def esperar_turno(self, client):
        """/api/chat hasta que el hilo salga de "pensando" (o revienta)."""
        for _ in range(200):
            estado = client.get("/api/chat").json()
            if estado["estado"] != "pensando":
                return estado
            time.sleep(0.01)
        self.fail("El turno del chat se quedó pensando demasiado")

    def test_turno_completo_y_forma_de_acciones(self):
        llamadas = self.falsificar_responder({
            "respuesta": "Buena señal, sigue así.",
            "registrar": "Empieza a estudiar a las 16:30.",
            "aprender": "Estudia mejor por la mañana.",
        })
        client = TestClient(servidor.app)
        r = client.post("/api/chat", json={"texto": "Me pongo a estudiar"})
        self.assertEqual(r.status_code, 200, r.text)
        estado = self.esperar_turno(client)
        # El turno llegó al modelo con el texto limpio y el config real.
        self.assertEqual(llamadas[0]["texto"], "Me pongo a estudiar")
        self.assertIsNotNone(llamadas[0]["intentos"])
        # La conversación: primero lo de Antonio, luego el agente.
        autores = [m["autor"] for m in estado["mensajes"]]
        self.assertEqual(autores, ["antonio", "agente"], estado["mensajes"])
        agente = estado["mensajes"][-1]
        self.assertEqual(agente["texto"], "Buena señal, sigue así.")
        # La FORMA EXACTA que pinta la web: lista [{hecho, texto}], una por
        # acción y en el orden registrar→aprender (solo las que hay).
        self.assertEqual(agente["acciones"], [
            {"hecho": "registrar", "texto": "Empieza a estudiar a las 16:30."},
            {"hecho": "aprender", "texto": "Estudia mejor por la mañana."},
        ])
        self.assertEqual(self.aprendido, ["Estudia mejor por la mañana."])
        self.assertEqual(estado["estado"], "listo")
        self.assertIsNone(estado["error"])

    def test_turno_sin_acciones_lleva_lista_vacia(self):
        self.falsificar_responder({"respuesta": "Entendido.",
                                   "registrar": None, "aprender": None})
        client = TestClient(servidor.app)
        client.post("/api/chat", json={"texto": "nada que registrar"})
        estado = self.esperar_turno(client)
        self.assertEqual(estado["mensajes"][-1]["acciones"], [])
        self.assertEqual(self.aprendido, [])

    def test_409_en_turno_en_marcha(self):
        # Un responder que "piensa" hasta que lo aviso: el turno está de
        # verdad en el hilo, no simulado a mano.
        liberar = threading.Event()
        self.falsificar_responder(
            {"respuesta": "Tarde pero llega.", "registrar": None, "aprender": None},
            espera=liberar,
        )
        client = TestClient(servidor.app)
        r = client.post("/api/chat", json={"texto": "primero"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(client.get("/api/chat").json()["estado"], "pensando")
        # Segundo turno mientras el primero no termina: 409.
        r = client.post("/api/chat", json={"texto": "segundo en medio"})
        self.assertEqual(r.status_code, 409, r.text)
        # El segundo no entró en la sala ni llegó al modelo.
        estado = client.get("/api/chat").json()
        self.assertEqual([m["texto"] for m in estado["mensajes"]], ["primero"])
        liberar.set()  # dejamos que el primero acabe y el estado no cuelgue
        estado = self.esperar_turno(client)
        self.assertEqual(estado["estado"], "listo")

    def test_arranque_roto_no_deja_pensando_eterno(self):
        # Regresión del hallazgo: si guardar el mensaje de Antonio falla
        # ANTES de arrancar el hilo, el estado no puede quedarse en
        # "pensando" (sería un 409 sin fin) y el POST se entera (500).
        original = chat.guardar_sala

        def sala_rota(salida, autor, texto, **extras):
            if autor == "antonio":
                raise RuntimeError("almacén roto")
            return original(salida, autor, texto, **extras)

        chat.guardar_sala = sala_rota

        def restaurar():
            chat.guardar_sala = original
        self.addCleanup(restaurar)
        self.falsificar_responder({"respuesta": "ok", "registrar": None, "aprender": None})
        client = TestClient(servidor.app)
        r = client.post("/api/chat", json={"texto": "hola"})
        self.assertEqual(r.status_code, 500, r.text)
        with servidor.CANDADO_CHAT:
            colgado = servidor.ESTADO_CHAT["estado"] == "pensando"
        self.assertFalse(colgado, "el estado se quedó colgado en pensando")
        self.assertEqual(client.get("/api/chat").json()["estado"], "error")
        # Y queda recuperable: restauramos la sala y el turno siguiente NO
        # choca contra 409 (ni aunque el anterior se colgara).
        chat.guardar_sala = original
        r = client.post("/api/chat", json={"texto": "otra vez"})
        self.assertEqual(r.status_code, 200, r.text)
        estado = self.esperar_turno(client)
        self.assertEqual(estado["estado"], "listo")

    def test_error_en_el_hilo_deja_estado_recuperable(self):
        # El turno arranca y responder explota (ollama caído, JSON inválido):
        # estado "error" con el mensaje, un mensaje "error" en la sala y el
        # turno posterior se puede hacer (no 409 eterno).
        self.falsificar_responder(excepcion=RuntimeError("ollama no contesta"))
        client = TestClient(servidor.app)
        r = client.post("/api/chat", json={"texto": "mensaje que falla"})
        self.assertEqual(r.status_code, 200, r.text)
        estado = self.esperar_turno(client)
        self.assertEqual(estado["estado"], "error")
        self.assertIn("ollama no contesta", estado["error"])
        self.assertLessEqual(len(estado["error"]), 200)
        ultimo = estado["mensajes"][-1]
        self.assertEqual(ultimo["autor"], "error")
        self.assertIn("ollama no contesta", ultimo["texto"])
        # Recuperable: quitamos la falsificación rota y repetimos.
        self.falsificar_responder({"respuesta": "ahora sí",
                                   "registrar": None, "aprender": None})
        r = client.post("/api/chat", json={"texto": "otro turno"})
        self.assertEqual(r.status_code, 200, r.text)
        estado = self.esperar_turno(client)
        self.assertEqual(estado["estado"], "listo")

    def test_400_si_el_mensaje_va_vacio(self):
        client = TestClient(servidor.app)
        for texto in ("", "   "):
            r = client.post("/api/chat", json={"texto": texto})
            self.assertEqual(r.status_code, 400, r.text)
        # Ni mensaje guardado ni marca "pensando" colgada.
        estado = client.get("/api/chat").json()
        self.assertEqual(estado["mensajes"], [])
        self.assertEqual(estado["estado"], "idle")

    def test_get_devuelve_estado_y_conversacion(self):
        client = TestClient(servidor.app)
        estado = client.get("/api/chat").json()
        self.assertEqual(sorted(estado), ["error", "estado", "mensajes"])
        self.assertEqual(estado["estado"], "idle")
        self.assertIsNone(estado["error"])
        self.assertEqual(estado["mensajes"], [])


if __name__ == "__main__":
    unittest.main()