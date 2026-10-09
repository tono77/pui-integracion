"""Contrato con TUS sistemas internos. Es la única pieza que depende de tu negocio.

El manual deja la lógica de búsqueda 100% del lado de la institución (Anexo 1 y 3):
la PUI sólo manda la CURP y espera que le digas qué encontraste.

Implementa `FuenteDatos` contra tu core (cartera, originación, cobranza, CRM...).
`FuenteEnMemoria` sirve para pruebas y como ejemplo del formato esperado.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


@dataclass
class DatosPersona:
    """Los datos MÁS RECIENTES que tengas de la persona (fase 1)."""
    nombre: str | None = None
    primer_apellido: str | None = None
    segundo_apellido: str | None = None
    fecha_nacimiento: date | None = None
    sexo: str | None = None                 # H | M | X
    telefono: str | None = None
    correo: str | None = None
    domicilio: dict | None = None           # direccion, calle, numero, colonia, codigo_postal, municipio_o_alcaldia, entidad_federativa
    fotos: list[bytes] = field(default_factory=list)          # >= 300 ppi, <= 240 KB c/u
    formato_fotos: str | None = None        # "jpg", "png"...
    huellas: dict[str, bytes] = field(default_factory=dict)   # etiquetas del Anexo 4 (rone, ltwo, ...), >= 500 ppi, gris 8 bits
    formato_huellas: str | None = None      # "wsq"...

    def vacio(self) -> bool:
        return not any([
            self.nombre, self.primer_apellido, self.segundo_apellido, self.fecha_nacimiento, self.sexo,
            self.telefono, self.correo, self.domicilio, self.fotos, self.huellas,
        ])


@dataclass
class Evento:
    """Una operación administrativa que demuestra actividad de la persona (fases 2 y 3).

    Ejemplos en una financiera: solicitud de crédito, desembolso, pago en sucursal,
    actualización de domicilio o teléfono, firma de contrato, visita de un asesor.
    """
    clave: str                  # identificador ÚNICO y estable del evento en tu sistema (evita reenvíos)
    tipo: str                   # "Desembolso de crédito", "Pago en sucursal"...
    fecha: date                 # cuándo ocurrió
    modificado: datetime        # cuándo se creó/modificó el registro en tu base (para la fase 3)
    descripcion_lugar: str | None = None   # "Sucursal Lomas Estrella"
    direccion: dict | None = None          # mismas llaves que DatosPersona.domicilio


class FuenteDatos(Protocol):
    def datos_basicos(self, curp: str) -> DatosPersona | None:
        """Fase 1. None si la CURP no existe en tus bases."""

    def eventos_entre(self, curp: str, desde: date, hasta: date) -> list[Evento]:
        """Fase 2. Eventos con fecha en [desde, hasta]."""

    def eventos_modificados(self, curps: list[str], desde: datetime) -> list[tuple[str, Evento]]:
        """Fase 3, en lote: eventos creados o modificados después de `desde` para cualquiera
        de las CURPs dadas. Devuelve pares (curp, evento). Hazlo con UNA consulta indexada
        por CURP y fecha de modificación, no una por persona."""


class FuenteEnMemoria:
    def __init__(self):
        self.personas: dict[str, DatosPersona] = {}
        self.eventos: dict[str, list[Evento]] = {}

    def datos_basicos(self, curp):
        return self.personas.get(curp)

    def eventos_entre(self, curp, desde, hasta):
        return [e for e in self.eventos.get(curp, []) if desde <= e.fecha <= hasta]

    def eventos_modificados(self, curps, desde):
        return [(c, e) for c in curps for e in self.eventos.get(c, []) if e.modificado > desde]
