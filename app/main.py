"""API que la PUI invoca (sección 8) + controles de ciberseguridad (sección 10) + worker de fondo.

Arranque:  uvicorn app.main:app_desde_entorno --factory --host 0.0.0.0 --port 8443 --no-server-header \
               --ssl-keyfile key.pem --ssl-certfile cert.pem
(o detrás de un balanceador/reverse proxy que termine TLS 1.2+)
"""
import hmac
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

import jwt
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

from .busqueda import Motor
from .config import Config, validar_clave_entrada
from .db import BaseDatos
from .fuente import FuenteDatos, FuenteEnMemoria
from .pui_client import ClientePUI
from .schemas import ActivarReporte, DesactivarReporte, LoginEntrada

log = logging.getLogger("pui")

MAX_CUERPO = 64 * 1024  # los payloads de entrada son pequeños; cualquier cosa mayor se rechaza
ENCABEZADOS_SEGURIDAD = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class LimitadorTasa:
    """Ventana deslizante en memoria. Con más de una instancia, cámbialo por Redis o por el WAF."""

    def __init__(self, maximo: int, ventana_s: int = 60):
        self.maximo, self.ventana = maximo, ventana_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def permitir(self, clave: str) -> bool:
        ahora = time.monotonic()
        with self._lock:
            q = self._hits[clave]
            while q and ahora - q[0] > self.ventana:
                q.popleft()
            if len(q) >= self.maximo:
                return False
            q.append(ahora)
            return True


def _error(status: int, mensaje: str) -> JSONResponse:
    return JSONResponse({"error": mensaje}, status_code=status)


class Worker:
    """Ciclo de fondo: fases 1-2 de casos nuevos, envíos pendientes, fase 3 y resincronización."""

    def __init__(self, motor: Motor, cfg: Config, tick_s: float = 15):
        self.motor, self.cfg, self.tick = motor, cfg, tick_s
        self._parar = threading.Event()
        self._ult_continua = 0.0
        self._ult_resync = 0.0

    def iniciar(self):
        threading.Thread(target=self._bucle, name="pui-worker", daemon=True).start()

    def detener(self):
        self._parar.set()

    def _bucle(self):
        while not self._parar.is_set():
            ahora = time.monotonic()
            for nombre, paso in (
                ("resync", self._resync_si_toca),
                ("pendientes", self.motor.procesar_pendientes),
                ("continua", self._continua_si_toca),
                ("despachar", self.motor.despachar),
            ):
                try:
                    paso()
                except Exception:
                    log.exception("Fallo en paso %s del worker", nombre)
            self._parar.wait(max(1.0, self.tick - (time.monotonic() - ahora)))

    def _continua_si_toca(self):
        if time.monotonic() - self._ult_continua >= self.cfg.intervalo_continua_s:
            self.motor.ciclo_continua()
            self._ult_continua = time.monotonic()

    def _resync_si_toca(self):
        if time.monotonic() - self._ult_resync >= self.cfg.intervalo_resync_s:
            self._ult_resync = time.monotonic()  # aunque falle, no reintentar en cada tick
            self.motor.resincronizar()


def crear_app(
    cfg: Config | None = None,
    fuente: FuenteDatos | None = None,
    cliente: ClientePUI | None = None,
    iniciar_worker: bool = True,
) -> FastAPI:
    cfg = cfg or Config()
    if not validar_clave_entrada(cfg.entrada_clave):
        raise RuntimeError("PUI_ENTRADA_CLAVE no cumple la regla del manual (16-20, mayúscula, número, especial)")
    if len(cfg.jwt_secreto) < 32:
        raise RuntimeError("JWT_SECRETO debe tener al menos 32 caracteres")

    db = BaseDatos(cfg.db_path)
    motor = Motor(db, fuente or FuenteEnMemoria(), cfg, cliente or ClientePUI(cfg))
    worker = Worker(motor, cfg)
    limitador = LimitadorTasa(cfg.rate_limit_por_min)
    limitador_login = LimitadorTasa(5, 15 * 60)  # anti fuerza bruta: 5 intentos fallidos / 15 min por IP

    @asynccontextmanager
    async def ciclo_vida(_app):
        if iniciar_worker:
            worker.iniciar()
        yield
        worker.detener()

    # Sin /docs ni /openapi.json en producción: no exponer la superficie de la API
    app = FastAPI(lifespan=ciclo_vida, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.motor = db, motor

    @app.middleware("http")
    async def seguridad(request: Request, call_next):
        ip = request.client.host if request.client else "?"
        if not limitador.permitir(ip):
            db.log("SEGURIDAD", "rate-limit", None, "429", ip)
            resp = _error(429, "Demasiadas solicitudes")
        elif int(request.headers.get("content-length") or 0) > MAX_CUERPO:
            resp = _error(413, "Solicitud demasiado grande")
        else:
            try:
                resp = await call_next(request)
            except Exception:
                log.exception("Error no controlado")
                resp = _error(500, "Error interno del servidor")
        for k, v in ENCABEZADOS_SEGURIDAD.items():
            resp.headers[k] = v
        return resp

    # Errores sin detalles internos (sin stacktrace, sin valores recibidos)
    @app.exception_handler(RequestValidationError)
    async def _validacion(_req, exc: RequestValidationError):
        campos = sorted({".".join(str(p) for p in e["loc"][1:]) or "cuerpo" for e in exc.errors()})
        return JSONResponse({"error": "Solicitud inválida", "campos": campos}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req, exc: StarletteHTTPException):
        mensajes = {401: "No autenticado", 403: "Sin permisos", 404: "No encontrado", 405: "Método no permitido"}
        return _error(exc.status_code, mensajes.get(exc.status_code, "Error"))

    bearer = HTTPBearer(auto_error=False)

    def requiere_pui(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        if not cred or cred.scheme.lower() != "bearer":
            raise StarletteHTTPException(401)
        try:
            reclamos = jwt.decode(
                cred.credentials, cfg.jwt_secreto, algorithms=["HS256"],
                audience=cfg.institucion_rfc, options={"require": ["exp", "iat", "sub", "aud", "jti"]},
            )
        except jwt.PyJWTError:
            raise StarletteHTTPException(401)
        if reclamos["sub"] != "PUI":
            raise StarletteHTTPException(403)
        return reclamos["jti"]

    # ---------- 8.1 /login ----------
    @app.post("/login")
    def login(cuerpo: LoginEntrada, request: Request):
        ip = request.client.host if request.client else "?"
        if not hmac.compare_digest(cuerpo.clave.encode(), cfg.entrada_clave.encode()):
            db.log("SEGURIDAD", "login", None, "FALLIDO", ip)
            if not limitador_login.permitir(ip):
                return _error(429, "Demasiados intentos")
            return _error(401, "Credenciales inválidas")
        ahora = int(time.time())
        token = jwt.encode(
            {"sub": "PUI", "aud": cfg.institucion_rfc, "iat": ahora, "exp": ahora + cfg.jwt_vigencia_s,
             "jti": uuid.uuid4().hex},
            cfg.jwt_secreto, algorithm="HS256",
        )
        db.log("SEGURIDAD", "login", None, "OK")
        return {"token": token}

    # ---------- 8.2 /activar-reporte ----------
    # Responde de inmediato y procesa en segundo plano: la búsqueda nunca debe provocar un 504.
    @app.post("/activar-reporte")
    def activar_reporte(cuerpo: ActivarReporte, _jti: str = Depends(requiere_pui)):
        nuevo = db.activar(cuerpo.model_dump(exclude_none=True))
        db.log("ENTRADA", "activar-reporte", cuerpo.id, "NUEVO" if nuevo else "DUPLICADO")
        return {"message": "La solicitud de activación del reporte de búsqueda se recibió correctamente."}

    # ---------- 8.3 /activar-reporte-prueba ----------
    @app.post("/activar-reporte-prueba")
    def activar_reporte_prueba(cuerpo: ActivarReporte, _jti: str = Depends(requiere_pui)):
        db.activar(cuerpo.model_dump(exclude_none=True), es_prueba=True)  # se guarda, nunca se procesa
        db.log("ENTRADA", "activar-reporte-prueba", cuerpo.id, "OK")
        return {"message": "La solicitud de activación del reporte de búsqueda se recibió correctamente."}

    # ---------- 8.4 /desactivar-reporte ----------
    @app.post("/desactivar-reporte")
    def desactivar_reporte(cuerpo: DesactivarReporte, _jti: str = Depends(requiere_pui)):
        cambio = db.desactivar(cuerpo.id)
        db.log("ENTRADA", "desactivar-reporte", cuerpo.id, "DESACTIVADO" if cambio else "NO_ACTIVO")
        # Texto tal cual lo trae el manual (parece copiado de 7.3; confirmar con la PUI)
        return {"message": "Registro de finalización de búsqueda histórica guardado correctamente"}

    return app


def app_desde_entorno() -> FastAPI:
    """Fábrica para uvicorn (--factory). Aquí conectas tu implementación real de FuenteDatos, p. ej.:
        from mi_core.pui import FuenteCore
        return crear_app(fuente=FuenteCore(...))
    """
    return crear_app()
