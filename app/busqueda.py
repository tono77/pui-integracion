"""Motor de búsqueda en 3 fases (sección 6, flujo del proceso, paso 4) + envío a la PUI + resincronización."""
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timezone

from pydantic import ValidationError

from . import cifrado
from .config import Config
from .curp import lugar_nacimiento
from .db import BaseDatos, ahora
from .fuente import DatosPersona, Evento, FuenteDatos
from .pui_client import ClientePUI
from .schemas import ETIQUETAS_HUELLAS, ActivarReporte, Coincidencia

log = logging.getLogger("pui.motor")

_PERMITIDO_TEXTO = set("ÁÉÍÓÚÜÑáéíóúüñ .,#'/:()-")
_PERMITIDO_NOMBRE = set("ÁÉÍÓÚÜÑáéíóúüñ '-")
_CORREO = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,50}$")


def limpiar(valor, maximo: int, nombre: bool = False) -> str | None:
    """Ajusta texto de tus sistemas a las expresiones regulares del manual (quita lo no permitido,
    convierte acentos raros a su letra base y recorta). Así un '&' o un 'Nº' no tumba el envío."""
    if valor is None:
        return None
    extra = _PERMITIDO_NOMBRE if nombre else _PERMITIDO_TEXTO
    salida = []
    for ch in unicodedata.normalize("NFC", str(valor)):
        if (ch.isascii() and ch.isalpha()) or (not nombre and ch.isascii() and ch.isdigit()) or ch in extra:
            salida.append(ch)
            continue
        base = unicodedata.normalize("NFKD", ch)[:1]  # "é"->"e", "º"->"o"
        salida.append(base if base and base.isascii() and base.isalpha() else " ")
    texto = re.sub(r"\s+", " ", "".join(salida)).strip()[:maximo].strip()
    return texto or None


def _direccion(d: dict | None) -> dict | None:
    if not d:
        return None
    limites = {"direccion": 500, "calle": 50, "numero": 20, "colonia": 50, "municipio_o_alcaldia": 100, "entidad_federativa": 40}
    salida = {k: limpiar(d.get(k), m) for k, m in limites.items()}
    cp = re.sub(r"\D", "", str(d.get("codigo_postal") or ""))
    salida["codigo_postal"] = cp if len(cp) == 5 else None
    salida = {k: v for k, v in salida.items() if v}
    return salida or None


def _como_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _restar_anios(d: date, anios: int) -> date:
    try:
        return d.replace(year=d.year - anios)
    except ValueError:  # 29 de febrero
        return d.replace(year=d.year - anios, day=28)


class Motor:
    def __init__(self, db: BaseDatos, fuente: FuenteDatos, config: Config, cliente: ClientePUI):
        self.db, self.fuente, self.cfg, self.cliente = db, fuente, config, cliente

    # ---------- armado del payload ----------

    def _base(self, reporte_id: str, curp: str, fase: str, persona: DatosPersona | None, biometricos: bool) -> dict:
        datos: dict = {
            "id": reporte_id,
            "institucion_id": self.cfg.institucion_rfc,
            "curp": curp,
            "lugar_nacimiento": lugar_nacimiento(curp),
            "fase_busqueda": fase,
        }
        if not persona:
            return datos
        nombre = {
            "nombre": limpiar(persona.nombre, 50, nombre=True),
            "primer_apellido": limpiar(persona.primer_apellido, 50, nombre=True),
            "segundo_apellido": limpiar(persona.segundo_apellido, 50, nombre=True),
        }
        nombre = {k: v for k, v in nombre.items() if v}
        if nombre:
            datos["nombre_completo"] = nombre
        if persona.fecha_nacimiento:
            datos["fecha_nacimiento"] = persona.fecha_nacimiento.isoformat()
        if persona.sexo in ("H", "M", "X"):
            datos["sexo_asignado"] = persona.sexo
        if persona.telefono:
            tel = re.sub(r"[^\d+]", "", persona.telefono)
            tel = ("+" if tel.startswith("+") else "") + tel.replace("+", "")
            if 0 < len(tel.lstrip("+")) <= 15:
                datos["telefono"] = tel
        if persona.correo and len(persona.correo) <= 50 and _CORREO.match(persona.correo):
            datos["correo"] = persona.correo
        if dom := _direccion(persona.domicilio):
            datos["domicilio"] = dom
        # Biométricos sólo en fase 1 (minimización de datos) y sólo si hay clave configurada
        if biometricos and self.cfg.clave_biometricos:
            if persona.fotos:
                datos["fotos"] = [cifrado.cifrar(f, self.cfg.clave_biometricos) for f in persona.fotos]
                datos["formato_fotos"] = (persona.formato_fotos or "jpg")[:20]
            huellas = {k: v for k, v in persona.huellas.items() if k in ETIQUETAS_HUELLAS}
            if huellas:
                datos["huellas"] = {k: cifrado.cifrar(v, self.cfg.clave_biometricos) for k, v in huellas.items()}
                datos["formato_huellas"] = (persona.formato_huellas or "wsq")[:50]
        return datos

    def _con_evento(self, datos: dict, ev: Evento) -> dict:
        datos = dict(datos)
        if t := limpiar(ev.tipo, 500):
            datos["tipo_evento"] = t
        datos["fecha_evento"] = ev.fecha.isoformat()
        if lugar := limpiar(ev.descripcion_lugar, 500):
            datos["descripcion_lugar_evento"] = lugar
        if dir_ev := _direccion(ev.direccion):
            datos["direccion_evento"] = dir_ev
        return datos

    def _encolar_coincidencia(self, reporte_id: str, huella: str, datos: dict) -> bool:
        try:
            payload = Coincidencia(**datos).payload()
        except ValidationError as e:
            # No debería pasar tras limpiar(); si pasa, se registra sin datos personales
            campos = ",".join(str(err["loc"][0]) for err in e.errors())
            self.db.log("SALIDA", "coincidencia-invalida", reporte_id, "DESCARTADA", f"campos: {campos}")
            return False
        return self.db.encolar(huella, reporte_id, "notificar-coincidencia", payload)

    # ---------- fases 1 y 2 para reportes recién activados ----------

    def procesar_pendientes(self):
        for r in self.db.reportes_en("PENDIENTE"):
            try:
                self._fases_1_y_2(r["id"], r["curp"], json.loads(r["datos"]))
            except Exception as e:  # un caso con error no detiene a los demás; se reintenta en el siguiente ciclo
                log.exception("Error procesando %s", r["id"])
                self.db.log("CONSULTA_INTERNA", "fases-1-2", r["id"], "ERROR", type(e).__name__)

    def _fases_1_y_2(self, reporte_id: str, curp: str, datos_reporte: dict):
        # Fase 1: completar datos básicos (un solo reporte, o ninguno si no hay nada)
        persona = self.fuente.datos_basicos(curp)
        self.db.log("CONSULTA_INTERNA", "fase-1", reporte_id, "ENCONTRADO" if persona else "SIN_REGISTRO")
        if persona and not persona.vacio():
            self._encolar_coincidencia(
                reporte_id, self.db.huella(reporte_id, "1"), self._base(reporte_id, curp, "1", persona, True)
            )

        # Fase 2: histórica, desde la desaparición hasta hoy, máximo 12 años
        corte = ahora()
        hoy = date.today()
        fd = datos_reporte.get("fecha_desaparicion")
        if fd:
            desde = max(date.fromisoformat(fd), _restar_anios(hoy, self.cfg.max_anios_historico))
            eventos = self.fuente.eventos_entre(curp, desde, hoy)
            self.db.log("CONSULTA_INTERNA", "fase-2", reporte_id, f"{len(eventos)} eventos")
            base = self._base(reporte_id, curp, "2", persona, False)
            for ev in eventos:
                self._encolar_coincidencia(reporte_id, self.db.huella(reporte_id, "2", ev.clave), self._con_evento(base, ev))
        else:
            # Sin fecha de desaparición se toma "hoy" y la fase 2 se omite (manual, paso 4.b)
            self.db.log("CONSULTA_INTERNA", "fase-2", reporte_id, "OMITIDA_SIN_FECHA")

        # Siempre se avisa el fin de la histórica, haya o no coincidencias
        self.db.encolar(
            self.db.huella(reporte_id, "fin"), reporte_id, "busqueda-finalizada",
            {"id": reporte_id, "institucion_id": self.cfg.institucion_rfc},
        )
        # A partir de aquí entra a búsqueda continua; sólo verá cambios posteriores a `corte`
        self.db.cambiar_estado(reporte_id, "HISTORICA_OK", marca_continua=corte)

    # ---------- fase 3: búsqueda continua ----------

    def ciclo_continua(self):
        activos = self.db.reportes_en("HISTORICA_OK")
        if not activos:
            return
        corte = ahora()
        desde = min(datetime.fromisoformat(r["marca_continua"]) for r in activos)
        por_curp: dict[str, list] = {}
        for r in activos:
            por_curp.setdefault(r["curp"], []).append(r)

        hallazgos = self.fuente.eventos_modificados(list(por_curp), desde)
        self.db.log("CONSULTA_INTERNA", "fase-3", None, f"{len(activos)} casos, {len(hallazgos)} eventos")

        personas: dict[str, DatosPersona | None] = {}
        for curp, ev in hallazgos:
            modificado = _como_utc(ev.modificado)
            for r in por_curp.get(curp, []):
                if modificado <= datetime.fromisoformat(r["marca_continua"]):
                    continue
                if curp not in personas:
                    personas[curp] = self.fuente.datos_basicos(curp)
                base = self._base(r["id"], curp, "3", personas[curp], False)
                # La huella incluye la fecha de modificación: si el registro cambia, se vuelve a reportar
                huella = self.db.huella(r["id"], "3", ev.clave, modificado.isoformat())
                self._encolar_coincidencia(r["id"], huella, self._con_evento(base, ev))

        for r in activos:
            self.db.actualizar_marca(r["id"], corte)

    # ---------- envío a la PUI (outbox con reintentos, en orden) ----------

    def despachar(self) -> int:
        enviados = 0
        for envio in self.db.pendientes():
            payload = json.loads(envio["payload"])
            if envio["endpoint"] == "busqueda-finalizada":
                res = self.cliente.busqueda_finalizada(payload)
            else:
                res = self.cliente.notificar_coincidencia(payload)
            if res.ok:
                self.db.marcar_envio(envio["huella"], "ENVIADO")
                self.db.log("SALIDA", envio["endpoint"], envio["reporte_id"], "200")
                enviados += 1
            elif res.permanente:
                self.db.marcar_envio(envio["huella"], "RECHAZADO", res.detalle)
                self.db.log("SALIDA", envio["endpoint"], envio["reporte_id"], "RECHAZADO", res.detalle)
            else:
                # PUI caída o error transitorio: se deja pendiente y se corta para conservar el orden
                self.db.marcar_envio(envio["huella"], "PENDIENTE", res.detalle)
                self.db.log("SALIDA", envio["endpoint"], envio["reporte_id"], "REINTENTO", res.detalle)
                break
        return enviados

    # ---------- resincronización (requisito: "mecanismo de resincronización") ----------

    def resincronizar(self):
        """Si estuvimos caídos y nos perdimos un /activar-reporte, lo recuperamos de GET /reportes."""
        remotos = self.cliente.reportes()
        locales = self.db.ids_activos()
        campos = set(ActivarReporte.model_fields)
        nuevos = 0
        for rep in remotos:
            if rep.get("id") in locales:
                continue
            try:
                datos = ActivarReporte(**{k: v for k, v in rep.items() if k in campos and v is not None})
            except ValidationError:
                self.db.log("ENTRADA", "resync", rep.get("id"), "INVALIDO")
                continue
            if self.db.activar(datos.model_dump(exclude_none=True)):
                nuevos += 1
                self.db.log("ENTRADA", "resync", datos.id, "RECUPERADO")
        # Casos activos aquí que ya no aparecen allá: posible /desactivar-reporte perdido.
        # No se desactivan solos: confirmar con la PUI la semántica de GET /reportes antes de automatizarlo.
        huerfanos = locales - {r.get("id") for r in remotos}
        if huerfanos:
            self.db.log("ENTRADA", "resync", None, "REVISAR", f"{len(huerfanos)} activos locales no listados por la PUI")
        return nuevos
