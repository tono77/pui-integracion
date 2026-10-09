"""Configuración del servicio. Todo sale de variables de entorno; nunca de código."""
import os
from dataclasses import dataclass, field


def _req(nombre: str, default: str | None = None) -> str:
    valor = os.environ.get(nombre, default)
    if valor is None:
        raise RuntimeError(f"Falta la variable de entorno {nombre}")
    return valor


@dataclass(frozen=True)
class Config:
    # Identidad de la institución ante la PUI (RFC con homoclave, Anexo 2)
    institucion_rfc: str = field(default_factory=lambda: _req("PUI_INSTITUCION_RFC"))

    # --- Salida: nosotros -> PUI (sección 7) ---
    pui_base_url: str = field(default_factory=lambda: _req("PUI_BASE_URL"))  # URL real: la entrega RENAPO/ATDT
    pui_clave: str = field(default_factory=lambda: _req("PUI_CLAVE"))  # clave que da el área técnica de la PUI
    pui_timeout_s: float = field(default_factory=lambda: float(os.environ.get("PUI_TIMEOUT_S", "20")))

    # --- Entrada: PUI -> nosotros (sección 8) ---
    # Contraseña que la PUI usará en nuestro /login (16-20 chars, mayúscula, número y especial)
    entrada_clave: str = field(default_factory=lambda: _req("PUI_ENTRADA_CLAVE"))
    jwt_secreto: str = field(default_factory=lambda: _req("JWT_SECRETO"))
    jwt_vigencia_s: int = field(default_factory=lambda: int(os.environ.get("JWT_VIGENCIA_S", "3600")))

    # Clave para cifrar biométricos (sección 9). Vacía = no se envían fotos/huellas.
    clave_biometricos: str = field(default_factory=lambda: os.environ.get("PUI_CLAVE_BIOMETRICOS", ""))

    # --- Operación ---
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "pui.db"))
    intervalo_continua_s: int = field(default_factory=lambda: int(os.environ.get("INTERVALO_CONTINUA_S", "3600")))
    intervalo_resync_s: int = field(default_factory=lambda: int(os.environ.get("INTERVALO_RESYNC_S", "21600")))
    max_anios_historico: int = 12  # art. 22, fr. II de los Lineamientos
    rate_limit_por_min: int = field(default_factory=lambda: int(os.environ.get("RATE_LIMIT_POR_MIN", "120")))


def validar_clave_entrada(clave: str) -> bool:
    """Regla del manual 8.1 para la contraseña del usuario PUI."""
    especiales = set("!@#$%^&*()-_.+")
    return (
        16 <= len(clave) <= 20
        and any(c.isupper() for c in clave)
        and any(c.isdigit() for c in clave)
        and any(c in especiales for c in clave)
    )
