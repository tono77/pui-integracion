"""Cliente de los endpoints que expone la PUI (sección 7)."""
import time
from dataclasses import dataclass

import httpx
import jwt

from .config import Config


@dataclass
class Resultado:
    ok: bool
    permanente: bool = False   # True = no tiene caso reintentar (400, 300...)
    detalle: str = ""


class ClientePUI:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.cfg = config
        self.http = http or httpx.Client(base_url=config.pui_base_url, timeout=config.pui_timeout_s, verify=True)
        self._token: str | None = None
        self._renovar_en = 0.0

    # 7.1 Acceso. El token dura 1 h; el manual recomienda renovarlo antes del 80 %.
    def _login(self):
        r = self.http.post("/login", json={"institucion_id": self.cfg.institucion_rfc, "clave": self.cfg.pui_clave})
        if r.status_code != 200:
            raise PermissionError(f"Login PUI falló con HTTP {r.status_code}")
        self._token = r.json()["token"]
        emitido = time.time()
        try:
            reclamos = jwt.decode(self._token, options={"verify_signature": False})
            vida = reclamos["exp"] - reclamos.get("iat", emitido)
        except Exception:
            vida = 3600
        self._renovar_en = emitido + 0.8 * vida

    def _headers(self, forzar: bool = False) -> dict:
        if forzar or not self._token or time.time() >= self._renovar_en:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, ruta: str, payload: dict) -> Resultado:
        try:
            r = self.http.post(ruta, json=payload, headers=self._headers())
            if r.status_code == 401:  # token vencido o revocado: un solo reintento con token nuevo
                r = self.http.post(ruta, json=payload, headers=self._headers(forzar=True))
        except (httpx.TransportError, PermissionError) as e:
            return Resultado(False, False, f"{type(e).__name__}")
        if r.status_code == 200:
            return Resultado(True)
        if r.status_code in (300, 400):
            # 300 = la PUI identificó a más de una persona; 400 = datos inválidos. Reintentar no cambia nada.
            return Resultado(False, True, f"HTTP {r.status_code}: {r.text[:300]}")
        return Resultado(False, False, f"HTTP {r.status_code}")

    def notificar_coincidencia(self, payload: dict) -> Resultado:  # 7.2
        return self._post("/notificar-coincidencia", payload)

    def busqueda_finalizada(self, payload: dict) -> Resultado:  # 7.3
        return self._post("/busqueda-finalizada", payload)

    def reportes(self) -> list[dict]:  # 7.4
        r = self.http.get("/reportes", headers=self._headers())
        if r.status_code == 401:
            r = self.http.get("/reportes", headers=self._headers(forzar=True))
        r.raise_for_status()
        return r.json()
