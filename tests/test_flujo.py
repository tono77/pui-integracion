from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import cifrado
from app.config import Config
from app.curp import lugar_nacimiento
from app.fuente import DatosPersona, Evento, FuenteEnMemoria
from app.main import crear_app
from app.pui_client import ClientePUI, Resultado

CURP = "PEPJ900101HDFRRN09"
ID = "e7b5a4c2-9f4e-4a99-91a2-6d4a8a1eaf3d-550e8400-e29b-41d4-a716-446655440000"
CLAVE_ENTRADA = "Prueba#Segura2026x"


class PUIFalsa:
    """Registra lo que le enviamos; puede simular caídas."""

    def __init__(self):
        self.recibidos: list[tuple[str, dict]] = []
        self.caida = False
        self.lista: list[dict] = []

    def _r(self, ep, p):
        if self.caida:
            return Resultado(False, False, "ConnectError")
        self.recibidos.append((ep, p))
        return Resultado(True)

    def notificar_coincidencia(self, p):
        return self._r("notificar-coincidencia", p)

    def busqueda_finalizada(self, p):
        return self._r("busqueda-finalizada", p)

    def reportes(self):
        return self.lista


@pytest.fixture
def entorno(tmp_path):
    cfg = Config(
        institucion_rfc="FIN010101AB1", pui_base_url="https://pui.test", pui_clave="x",
        entrada_clave=CLAVE_ENTRADA, jwt_secreto="s" * 40, clave_biometricos="biokey",
        db_path=str(tmp_path / "t.db"),
    )
    fuente, pui = FuenteEnMemoria(), PUIFalsa()
    app = crear_app(cfg, fuente, pui, iniciar_worker=False)
    return TestClient(app), app, fuente, pui


def _token(c):
    r = c.post("/login", json={"usuario": "PUI", "clave": CLAVE_ENTRADA})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_y_autenticacion(entorno):
    c, *_ = entorno
    assert c.post("/login", json={"usuario": "PUI", "clave": "Incorrecta#12345678"}).status_code == 401
    assert c.post("/activar-reporte", json={"id": ID, "curp": CURP}).status_code == 401
    assert c.post("/activar-reporte", json={"id": ID, "curp": CURP},
                  headers={"Authorization": "Bearer basura"}).status_code == 401


def test_fuerza_bruta_bloquea(entorno):
    c, *_ = entorno
    codigos = [c.post("/login", json={"usuario": "PUI", "clave": "Incorrecta#12345678"}).status_code for _ in range(7)]
    assert codigos[-1] == 429


def test_validacion_y_seguridad_http(entorno):
    c, *_ = entorno
    h = _token(c)
    r = c.post("/activar-reporte", json={"id": "corto", "curp": "abc"}, headers=h)
    assert r.status_code == 400 and "abc" not in r.text  # no refleja valores recibidos
    assert c.post("/activar-reporte", json={"id": ID, "curp": CURP, "extra": 1}, headers=h).status_code == 400
    assert c.get("/activar-reporte", headers=h).status_code == 405
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert c.get("/docs").status_code == 404


def test_flujo_completo_tres_fases(entorno):
    c, app, fuente, pui = entorno
    motor, db = app.state.motor, app.state.db
    hace_un_anio = date.today() - timedelta(days=365)
    fuente.personas[CURP] = DatosPersona(
        nombre="José", primer_apellido="Pérez", segundo_apellido="O'Neil & Cía",
        sexo="H", telefono="(55) 1234-5678", domicilio={"calle": "Av. Juárez Nº 5 «A»", "codigo_postal": "06000"},
        fotos=[b"\xff\xd8foto"], formato_fotos="jpg", huellas={"rone": b"wsq1", "zzz": b"ignorada"},
    )
    fuente.eventos[CURP] = [
        Evento("ev-viejo", "Pago en sucursal", hace_un_anio - timedelta(days=400),
               datetime(2020, 1, 1, tzinfo=timezone.utc)),
        Evento("ev-1", "Desembolso de crédito", hace_un_anio + timedelta(days=30),
               datetime(2025, 1, 1, tzinfo=timezone.utc), "Sucursal Centro", {"calle": "Madero"}),
    ]

    h = _token(c)
    cuerpo = {"id": ID, "curp": CURP, "fecha_desaparicion": hace_un_anio.isoformat()}
    assert c.post("/activar-reporte", json=cuerpo, headers=h).status_code == 200
    assert c.post("/activar-reporte", json=cuerpo, headers=h).status_code == 200  # duplicado: idempotente

    motor.procesar_pendientes()
    motor.despachar()
    tipos = [(ep, p.get("fase_busqueda")) for ep, p in pui.recibidos]
    assert tipos == [("notificar-coincidencia", "1"), ("notificar-coincidencia", "2"), ("busqueda-finalizada", None)]

    f1, f2 = pui.recibidos[0][1], pui.recibidos[1][1]
    assert f1["lugar_nacimiento"] == "CDMX"
    assert f1["nombre_completo"]["segundo_apellido"] == "O'Neil Cía"  # '&' fuera, acentos válidos se quedan; saneado a la regex del manual
    assert f1["telefono"] == "5512345678"
    assert f1["domicilio"]["calle"] == "Av. Juárez No 5 A"
    assert set(f1["huellas"]) == {"rone"}
    assert cifrado.descifrar(f1["fotos"][0], "biokey") == b"\xff\xd8foto"
    assert "tipo_evento" not in f1
    assert f2["tipo_evento"] == "Desembolso de crédito" and "fotos" not in f2

    # Fase 3: aparece un evento nuevo
    pui.recibidos.clear()
    motor.ciclo_continua()
    motor.despachar()
    assert pui.recibidos == []
    fuente.eventos[CURP].append(
        Evento("ev-2", "Actualización de domicilio", date.today(), datetime.now(timezone.utc) + timedelta(seconds=5))
    )
    motor.ciclo_continua()
    motor.despachar()
    assert [p["fase_busqueda"] for _, p in pui.recibidos] == ["3"]

    # Desactivar: ya no se busca ni se envía nada
    pui.recibidos.clear()
    assert c.post("/desactivar-reporte", json={"id": ID}, headers=h).status_code == 200
    fuente.eventos[CURP].append(
        Evento("ev-3", "Pago", date.today(), datetime.now(timezone.utc) + timedelta(seconds=10))
    )
    motor.ciclo_continua()
    motor.despachar()
    assert pui.recibidos == []


def test_sin_fecha_desaparicion_omite_fase2_pero_finaliza(entorno):
    c, app, fuente, pui = entorno
    h = _token(c)
    c.post("/activar-reporte", json={"id": ID, "curp": CURP}, headers=h)
    app.state.motor.procesar_pendientes()
    app.state.motor.despachar()
    assert [ep for ep, _ in pui.recibidos] == ["busqueda-finalizada"]  # sin datos: fase 1 se omite


def test_pui_caida_reintenta_en_orden(entorno):
    c, app, fuente, pui = entorno
    fuente.personas[CURP] = DatosPersona(nombre="Ana")
    c.post("/activar-reporte", json={"id": ID, "curp": CURP}, headers=_token(c))
    app.state.motor.procesar_pendientes()
    pui.caida = True
    assert app.state.motor.despachar() == 0
    pui.caida = False
    assert app.state.motor.despachar() == 2
    assert [ep for ep, _ in pui.recibidos] == ["notificar-coincidencia", "busqueda-finalizada"]


def test_reporte_de_prueba_no_se_procesa(entorno):
    c, app, fuente, pui = entorno
    fuente.personas[CURP] = DatosPersona(nombre="Ana")
    assert c.post("/activar-reporte-prueba", json={"id": ID, "curp": CURP}, headers=_token(c)).status_code == 200
    app.state.motor.procesar_pendientes()
    app.state.motor.despachar()
    assert pui.recibidos == []


def test_resync_recupera_reportes_perdidos(entorno):
    _, app, _, pui = entorno
    pui.lista = [{"id": ID, "curp": CURP, "nombre": "Ana", "fecha_registro": "2023-01-01", "lugar_nacimiento": "CDMX"}]
    assert app.state.motor.resincronizar() == 1
    assert app.state.motor.resincronizar() == 0


def test_lugar_nacimiento():
    assert lugar_nacimiento(CURP) == "CDMX"
    assert lugar_nacimiento("PEPJ900101HNERRN09") == "FORANEO"
    assert lugar_nacimiento("XXXX") == "DESCONOCIDO"


@respx.mock
def test_cliente_renueva_token_en_401():
    cfg = Config(institucion_rfc="FIN010101AB1", pui_base_url="https://pui.test", pui_clave="k",
                 entrada_clave=CLAVE_ENTRADA, jwt_secreto="s" * 40)
    login = respx.post("https://pui.test/login").mock(return_value=httpx.Response(200, json={"token": "a.b.c"}))
    respx.post("https://pui.test/notificar-coincidencia").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"message": "ok"})]
    )
    cli = ClientePUI(cfg, httpx.Client(base_url=cfg.pui_base_url))
    assert cli.notificar_coincidencia({"x": 1}).ok
    assert login.call_count == 2
