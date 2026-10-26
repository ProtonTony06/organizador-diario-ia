"""Tests del chat de contexto: parseo JSON, validación, perfil y sala.

Solo funciones puras y ficheros temporales: NADA de ollama (responder se
ejercita con _llamar falsificada). El contrato que se fija aquí: la
respuesta es un JSON con respuesta obligatoria y registrar/aprender
opcionales; el registro y los aprendizajes van a ficheros reales del
directorio del día (tmpdir en los tests, nunca a data/ del repo).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chat  # noqa: E402
import registro  # noqa: E402


def rutinas_ejemplo():
    """Un ancla y capacidad, igual de pequeñitos que en test_planificador."""
    return {
        "ventana": {"inicio": "07:30", "fin": "01:00"},
        "anclas": [{"momento": "14:30", "duracion": 90, "nombre": "Comida"}],
        "capacidades": {"dev": 150},
    }


class TestElegirRespuesta(unittest.TestCase):
    def test_json_limpio(self):
        elegido = chat._elegir_respuesta('{"respuesta": "Hola", "registrar": null, "aprender": null}')
        self.assertEqual(elegido["respuesta"], "Hola")

    def test_vallas_y_razonamiento_antes(self):
        texto = (
            "Veamos lo que dice Antonio...\n"
            '```json\n{"respuesta": "Suena bien", "registrar": "Empieza a estudiar", "aprender": null}\n```\n'
            "Otra idea:\n```json\n{\"respuesta\": \"La final\"}\n```"
        )
        elegido = chat._elegir_respuesta(texto)
        self.assertEqual(elegido["respuesta"], "La final")

    def test_defecto_de_puntitos(self):
        # "...," literales del razonamiento del modelo dentro de listas
        texto = '{"respuesta": "Va", "registrar": null, "aprender": null, "algo": ["uno", "..."]}'
        elegido = chat._elegir_respuesta(texto)
        self.assertIsNotNone(elegido)
        self.assertEqual(elegido["respuesta"], "Va")

    def test_sin_json(self):
        self.assertIsNone(chat._elegir_respuesta("Lo siento, no entiendo. ¿Puedes repetir?"))

    def test_respuesta_vacia_cuenta_como_no_elegible(self):
        self.assertIsNone(chat._elegir_respuesta('{"respuesta": ""}'))


class TestValidar(unittest.TestCase):
    def test_minimo_valido(self):
        self.assertEqual(chat._validar({"respuesta": "Bien, sigue así"}), [])

    def test_registrar_y_aprender_texto(self):
        r = {"respuesta": "OK", "registrar": "Empieza a estudiar bases de datos", "aprender": None}
        self.assertEqual(chat._validar(r), [])

    def test_null_literal_se_normaliza_a_none(self):
        # El modelo puede responder "null" COMO TEXTO (el JSON a veces lo
        # escapa): la validación lo acepta y lo pasa a None _normalizar
        # (la validación ya NO muta el dict que recibe, solo la copia).
        r = {"respuesta": "OK", "registrar": "null", "aprender": "none"}
        self.assertEqual(chat._validar(r), [])
        self.assertIsNone(chat._normalizar(r)["registrar"])
        self.assertIsNone(chat._normalizar(r)["aprender"])

    def test_respuesta_falta(self):
        self.assertTrue(chat._validar({"registrar": "X"}))

    def test_respuesta_vacia(self):
        self.assertTrue(chat._validar({"respuesta": "  "}))

    def test_campos_desconocidos_rechazados(self):
        # La fase de aprendizaje NO tiene cerrar/plan/etc: si el modelo los
        # emite son errores (re-prueba), no acciones silenciadas.
        errores = chat._validar({"respuesta": "OK", "cerrar": "abc123"})
        self.assertTrue(any("cerrar" in e for e in errores))
        errores = chat._validar({"respuesta": "OK", "plan": []})
        self.assertTrue(any("plan" in e for e in errores))

    def test_registrar_no_texto(self):
        self.assertTrue(chat._validar({"respuesta": "OK", "registrar": 42}))

    def test_registrar_demasiado_corto(self):
        self.assertTrue(chat._validar({"respuesta": "OK", "registrar": "est"}))

    def test_respuesta_no_dict(self):
        self.assertTrue(chat._validar(["no", "soy", "dict"]))

    def test_validar_no_muta_el_dict_recibido(self):
        # Regresión: la validación normalizaba los "null" mutando la
        # entrada — los reintentos pasaban el MISMO dict ya sembrado.
        r = {"respuesta": "OK", "registrar": "null", "aprender": None}
        chat._validar(r)
        self.assertEqual(r["registrar"], "null")


class TestPerfilYChat(unittest.TestCase):
    def setUp(self):
        self.carpeta = Path(tempfile.mkdtemp())
        self.perfil = self.carpeta / "perfil.md"
        (self.perfil.with_suffix(".lock")).unlink(missing_ok=True)

    def test_perfil_inexistente_da_vacio(self):
        self.assertEqual(chat.leer_perfil(self.carpeta / "nohay.md"), [])

    def test_aprendizaje_une_con_fecha(self):
        chat.guardar_aprendizaje("Estudia mejor por la mañana", self.perfil)
        lineas = chat.leer_perfil(self.perfil)
        self.assertEqual(len(lineas), 1)
        self.assertIn("(20", lineas[0])
        self.assertIn("Estudia mejor por la mañana", lineas[0])
        # Cabezera al crear el fichero: el fichero queda legible a mano
        contenido = self.perfil.read_text(encoding="utf-8")
        self.assertTrue(contenido.startswith("# Perfil"))

    def test_aprendizaje_duplicado_no_repite(self):
        chat.guardar_aprendizaje("Patrón estable", self.perfil)
        chat.guardar_aprendizaje("Patrón estable", self.perfil)
        self.assertEqual(len(chat.leer_perfil(self.perfil)), 1)

    def test_aprendizaje_no_duplica_por_subcadena(self):
        # Regresión: comparar por subcadena daba por aprendido "Estudia
        # mejor por la mañana" porque "Estudia" era subcadena suya.
        chat.guardar_aprendizaje("Estudia", self.perfil)
        chat.guardar_aprendizaje("Estudia mejor por la mañana", self.perfil)
        self.assertEqual(len(chat.leer_perfil(self.perfil)), 2)

    def test_aprendizaje_vacio_no_escribe(self):
        self.assertFalse(chat.guardar_aprendizaje("  ", self.perfil))
        self.assertFalse(self.perfil.exists())

    def test_aprendizajes_no_se_pisa_entre_hilos(self):
        # flock del perfil: 20 escrituras concurrentes no se pierden
        import threading
        hilos = [
            threading.Thread(target=chat.guardar_aprendizaje, args=(f"Patrón número {i}", self.perfil))
            for i in range(20)
        ]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        self.assertEqual(len(chat.leer_perfil(self.perfil)), 20)

    def test_sala_guarda_y_lee(self):
        chat.guardar_sala(self.carpeta, "antonio", "me pongo a estudiar")
        chat.guardar_sala(self.carpeta, "agente", "Bien, ¿qué toca hoy?")
        mensajes = chat.leer_chat(self.carpeta)
        self.assertEqual([m["autor"] for m in mensajes], ["antonio", "agente"])
        self.assertIn("texto", mensajes[0])
        self.assertIn("ts", mensajes[0])

    def test_sala_acepta_extras(self):
        chat.guardar_sala(self.carpeta, "agente", "Anotado", acciones=[{"hecho": "aprender", "texto": "X"}])
        mensajes = chat.leer_chat(self.carpeta)
        self.assertEqual(mensajes[0]["acciones"], [{"hecho": "aprender", "texto": "X"}])

    def test_sala_ignora_vacios(self):
        chat.guardar_sala(self.carpeta, "antonio", "  ")
        self.assertEqual(chat.leer_chat(self.carpeta), [])

    def test_chat_ilegible_no_tumba_la_lectura(self):
        (self.carpeta / "chat.jsonl").write_text(
            '{"autor": "antonio", "texto": "bueno"}\nESTA LINEA NO ES JSON\n'
        )
        mensajes = chat.leer_chat(self.carpeta)
        self.assertEqual(len(mensajes), 1)


class TestAplicar(unittest.TestCase):
    def setUp(self):
        self.carpeta = Path(tempfile.mkdtemp())

    def test_aplica_registrar(self):
        hecho = chat.aplicar(
            {"respuesta": "OK", "registrar": "Empieza a estudiar a las 16:30", "aprender": None},
            self.carpeta,
        )
        self.assertEqual(hecho, ["registrar"])
        eventos = registro.leer_eventos(self.carpeta)
        self.assertEqual(len(eventos), 1)
        self.assertEqual(eventos[0]["tipo"], "chat_registro")
        self.assertEqual(eventos[0]["texto"], "Empieza a estudiar a las 16:30")

    def test_aplica_aprender(self):
        perfil = self.carpeta / "perfil.md"
        hecho = chat.aplicar(
            {"respuesta": "OK", "registrar": None, "aprender": "Estudia por la mañana"},
            self.carpeta, perfil=perfil,
        )
        self.assertEqual(hecho, ["aprender"])
        self.assertIn("Estudia por la mañana", chat.leer_perfil(perfil).pop())

    def test_sin_acciones_devuelve_vacio(self):
        self.assertEqual(
            chat.aplicar({"respuesta": "Solo hablo", "registrar": None, "aprender": None}, self.carpeta),
            [],
        )
        self.assertEqual(registro.leer_eventos(self.carpeta), [])

    def test_aplica_registrar_y_aprender_a_la_vez(self):
        perfil = self.carpeta / "perfil.md"
        hecho = chat.aplicar(
            {"respuesta": "OK", "registrar": "Empieza a estudiar MasterD",
             "aprender": "Estudia después de comer"},
            self.carpeta, perfil=perfil,
        )
        # Registro al diario Y aprendizaje al perfil, en ese orden
        self.assertEqual(hecho, ["registrar", "aprender"])
        self.assertEqual(len(registro.leer_eventos(self.carpeta)), 1)
        self.assertEqual(len(chat.leer_perfil(perfil)), 1)


class TestResponder(unittest.TestCase):
    """responder() con _llamar falsificada: el contrato del bucle, sin ollama."""

    RUTINAS = rutinas_ejemplo()
    FECHA = "2026-10-26"

    def setUp(self):
        self.carpeta = Path(tempfile.mkdtemp())

    def test_respuesta_valida_directa(self):
        llamado = []
        def llamar(prompt, modelo):
            llamado.append((prompt, modelo))
            return json.dumps({
                "respuesta": "¡Suena bien!",
                "registrar": "Empieza a estudiar bases de datos",
                "aprender": None,
            })
        resultado = chat.responder(
            "me pongo a estudiar", self.RUTINAS, "modelo-falso",
            fecha=self.FECHA, _llamar=llamar,
        )
        self.assertEqual(resultado["registrar"], "Empieza a estudiar bases de datos")
        self.assertEqual(len(llamado), 1)
        # El prompt lleva el mensaje de Antonio y contextos de hoy
        self.assertIn("me pongo a estudiar", llamado[0][0])
        self.assertIn("FASE DE APRENDIZAJE", llamado[0][0])

    def test_json_malo_luego_bueno(self):
        def llamar(prompt, modelo):
            if "CORRECCIÓN OBLIGATORIA" not in prompt:
                return "Razono... pero no doy JSON."
            return '{"respuesta": "Ahora sí", "registrar": null, "aprender": null}'
        resultado = chat.responder(
            "vuelvo", self.RUTINAS, "modelo-falso", fecha=self.FECHA, _llamar=llamar
        )
        self.assertEqual(resultado["respuesta"], "Ahora sí")

    def test_acciones_invalidas_reintenta(self):
        # El modelo propone cerrar (acción de planificación PROHIBIDA en la
        # fase de aprendizaje): la validación lo rechaza y el reintento
        # (al que le llega el error como pista) lo corrige.
        def llamar(prompt, modelo):
            if "CORRECCIÓN OBLIGATORIA" in prompt:
                return '{"respuesta": "Sin cerrar nada", "registrar": null, "aprender": null}'
            return '{"respuesta": "Vamos", "cerrar": "abc123"}'
        resultado = chat.responder(
            "prueba", self.RUTINAS, "modelo-falso", fecha=self.FECHA, _llamar=llamar
        )
        self.assertEqual(resultado["respuesta"], "Sin cerrar nada")

    def test_agota_intentos_lanza(self):
        def llamar(prompt, modelo):
            return "no es json"
        with self.assertRaises(RuntimeError):
            chat.responder(
                "prueba", self.RUTINAS, "modelo-falso", fecha=self.FECHA,
                intentos=1, _llamar=llamar,
            )

    def test_prompt_lleva_perfil_y_conversacion_previa(self):
        perfil = self.carpeta / "perfil.md"
        chat.guardar_aprendizaje("Estudia con música clásica", perfil)
        chat.guardar_sala(self.carpeta, "antonio", "me toca el dentista")
        prompts = []
        def llamar(prompt, modelo):
            prompts.append(prompt)
            return '{"respuesta": "Suerte", "registrar": null, "aprender": null}'
        chat.responder(
            "pruebo el prompt", self.RUTINAS, "modelo-falso",
            fecha=self.FECHA, perfil=perfil, carpeta_dia=self.carpeta, _llamar=llamar,
        )
        prompt = prompts[0]
        # El contexto entra por el propio prompt: perfil y conversación previa
        self.assertIn("Estudia con música clásica", prompt)
        self.assertIn("me toca el dentista", prompt)
        self.assertIn("pruebo el prompt", prompt)


class TestPrompts(unittest.TestCase):
    def test_rutinas_al_prompt(self):
        texto = chat._hhmm_rutinas(rutinas_ejemplo())
        self.assertIn("Comida", texto)
        self.assertIn("14:30", texto)
        self.assertIn("16:00", texto)

    def test_rutinas_vacias(self):
        self.assertEqual(chat._hhmm_rutinas({}), "- (ninguna)")


if __name__ == "__main__":
    unittest.main()