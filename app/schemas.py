"""Modelos de datos con las reglas de validación del manual (secciones 7.2, 8.1, 8.2, 8.4)."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Expresiones regulares tal como vienen en el manual
TEXTO = r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 .,#'/:()\-]*$"
NOMBRE = r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ '\-]{1,50}$"
FECHA = r"^\d{4}-\d{2}-\d{2}$"

Id = Annotated[str, StringConstraints(min_length=36, max_length=75)]
Curp = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{18}$")]
Fecha = Annotated[str, StringConstraints(pattern=FECHA)]


def Texto(maximo: int):
    return Annotated[str, StringConstraints(max_length=maximo, pattern=TEXTO)]


class _Estricto(BaseModel):
    # El manual pide rechazar payloads inesperados (400/422)
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------- Entrada: lo que la PUI nos envía ----------

class LoginEntrada(_Estricto):
    usuario: Literal["PUI"]
    clave: Annotated[str, StringConstraints(min_length=16, max_length=20)]


class ActivarReporte(_Estricto):
    """8.2. Sólo id y curp vienen siempre; el resto puede omitirse o venir nulo."""
    id: Id
    curp: Curp
    nombre: Texto(50) | None = None
    primer_apellido: Texto(50) | None = None
    segundo_apellido: Texto(50) | None = None
    fecha_nacimiento: Fecha | None = None
    fecha_desaparicion: Fecha | None = None
    lugar_nacimiento: Annotated[str, StringConstraints(max_length=20)] | None = None
    sexo_asignado: Annotated[str, StringConstraints(pattern=r"^[MHX]$")] | None = None
    telefono: Annotated[str, StringConstraints(pattern=r"^\+?\d{0,15}$")] | None = None
    correo: Annotated[str, StringConstraints(max_length=50)] | None = None
    direccion: Texto(500) | None = None
    calle: Texto(50) | None = None
    numero: Texto(20) | None = None
    colonia: Texto(50) | None = None
    codigo_postal: Annotated[str, StringConstraints(pattern=r"^\d{0,5}$")] | None = None
    municipio_o_alcaldia: Texto(100) | None = None
    entidad_federativa: Texto(40) | None = None


class DesactivarReporte(_Estricto):
    id: Id


# ---------- Salida: lo que nosotros enviamos a la PUI ----------

class NombreCompleto(BaseModel):
    nombre: Annotated[str, StringConstraints(pattern=NOMBRE)] | None = None
    primer_apellido: Annotated[str, StringConstraints(pattern=NOMBRE)] | None = None
    segundo_apellido: Annotated[str, StringConstraints(pattern=NOMBRE)] | None = None


class Direccion(BaseModel):
    direccion: Texto(500) | None = None
    calle: Texto(50) | None = None
    numero: Texto(20) | None = None
    colonia: Texto(50) | None = None
    codigo_postal: Annotated[str, StringConstraints(pattern=r"^\d{0,5}$")] | None = None
    municipio_o_alcaldia: Texto(100) | None = None
    entidad_federativa: Texto(40) | None = None


class Coincidencia(BaseModel):
    """7.2 /notificar-coincidencia."""
    id: Id
    institucion_id: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{4,13}$")]
    curp: Curp
    lugar_nacimiento: Annotated[str, StringConstraints(max_length=20)]
    fase_busqueda: Literal["1", "2", "3"]
    nombre_completo: NombreCompleto | None = None
    fecha_nacimiento: Fecha | None = None
    sexo_asignado: Annotated[str, StringConstraints(pattern=r"^[MHX]$")] | None = None
    telefono: Annotated[str, StringConstraints(pattern=r"^\+?\d{0,15}$")] | None = None
    correo: Annotated[
        str, StringConstraints(max_length=50, pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,50}$")
    ] | None = None
    domicilio: Direccion | None = None
    fotos: list[str] | None = None
    formato_fotos: Annotated[str, StringConstraints(max_length=20)] | None = None
    huellas: dict[str, str] | None = None
    formato_huellas: Annotated[str, StringConstraints(max_length=50)] | None = None
    # Sólo fases 2 y 3
    tipo_evento: Texto(500) | None = None
    fecha_evento: Fecha | None = None
    descripcion_lugar_evento: Texto(500) | None = None
    direccion_evento: Direccion | None = None

    def payload(self) -> dict:
        datos = self.model_dump(exclude_none=True)
        if self.fase_busqueda == "1":
            for campo in ("tipo_evento", "fecha_evento", "descripcion_lugar_evento", "direccion_evento"):
                datos.pop(campo, None)
        return datos


ETIQUETAS_HUELLAS = {
    "rone", "rtwo", "rthree", "rfour", "rfive",
    "lone", "ltwo", "lthree", "lfour", "lfive",
    "rpalm", "lpalm",
}  # Anexo 4
