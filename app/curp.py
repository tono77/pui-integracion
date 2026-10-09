"""Anexo 5: código de entidad en la CURP (posiciones 12-13) -> valor de lugar_nacimiento."""
import re

ENTIDADES = {
    "AS": "AGUASCALIENTES", "BC": "BAJA CALIFORNIA", "BS": "BAJA CALIFORNIA SUR", "CC": "CAMPECHE",
    "CS": "CHIAPAS", "CH": "CHIHUAHUA", "DF": "CDMX", "CL": "COAHUILA", "CM": "COLIMA", "DG": "DURANGO",
    "GT": "GUANAJUATO", "GR": "GUERRERO", "HG": "HIDALGO", "JC": "JALISCO", "MC": "MÉXICO",
    "MN": "MICHOACÁN", "MS": "MORELOS", "NT": "NAYARIT", "NL": "NUEVO LEÓN", "OC": "OAXACA",
    "PL": "PUEBLA", "QO": "QUERÉTARO", "QR": "QUINTANA ROO", "SP": "SAN LUIS POTOSÍ", "SL": "SINALOA",
    "SR": "SONORA", "TC": "TABASCO", "TS": "TAMAULIPAS", "TL": "TLAXCALA", "VZ": "VERACRUZ",
    "YN": "YUCATÁN", "ZS": "ZACATECAS", "NE": "FORANEO",
}

_CURP = re.compile(r"^[A-Z]{4}\d{6}[HMX][A-Z]{2}[A-Z]{3}[A-Z0-9]\d$")


def lugar_nacimiento(curp: str | None) -> str:
    # 7.2: CURP inválida o código no reconocido -> DESCONOCIDO; NE -> FORANEO (sin acento en 7.2)
    if not curp or not _CURP.match(curp):
        return "DESCONOCIDO"
    return ENTIDADES.get(curp[11:13], "DESCONOCIDO")
