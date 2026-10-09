"""Persistencia local (requisito: "persistencia de búsquedas y coincidencias en bases de datos locales").

SQLite para el esqueleto; en producción cambia por PostgreSQL/SQL Server conservando el esquema.
Tablas:
  reportes      casos activos/desactivados y su avance por fase
  envios        outbox de notificaciones a la PUI (reintentos y no duplicar)
  bitacora      auditoría de todas las interacciones (Anexo 3: trazabilidad)
"""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

ESQUEMA = """
CREATE TABLE IF NOT EXISTS reportes (
    id                 TEXT PRIMARY KEY,
    curp               TEXT NOT NULL,
    datos              TEXT NOT NULL,          -- payload original de /activar-reporte
    estado             TEXT NOT NULL,          -- PENDIENTE | HISTORICA_OK | DESACTIVADO
    es_prueba          INTEGER NOT NULL DEFAULT 0,
    marca_continua     TEXT,                   -- último corte revisado en fase 3 (ISO 8601 UTC)
    creado             TEXT NOT NULL,
    actualizado        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reportes_estado ON reportes(estado);

CREATE TABLE IF NOT EXISTS envios (
    huella             TEXT PRIMARY KEY,       -- hash (id, fase, evento) para no reenviar lo mismo
    reporte_id         TEXT NOT NULL,
    endpoint           TEXT NOT NULL,          -- notificar-coincidencia | busqueda-finalizada
    payload            TEXT NOT NULL,
    estado             TEXT NOT NULL,          -- PENDIENTE | ENVIADO | RECHAZADO
    intentos           INTEGER NOT NULL DEFAULT 0,
    ultimo_error       TEXT,
    creado             TEXT NOT NULL,
    actualizado        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_envios_estado ON envios(estado);

CREATE TABLE IF NOT EXISTS bitacora (
    n                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha              TEXT NOT NULL,
    tipo               TEXT NOT NULL,          -- ENTRADA | CONSULTA_INTERNA | SALIDA | SEGURIDAD
    operacion          TEXT NOT NULL,
    reporte_id         TEXT,
    resultado          TEXT,
    detalle            TEXT                    -- sin datos sensibles (sección 10)
);
"""


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BaseDatos:
    def __init__(self, ruta: str):
        self._conn = sqlite3.connect(ruta, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(ESQUEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---------- reportes ----------

    def activar(self, datos: dict, es_prueba: bool = False) -> bool:
        """Idempotente: si el id ya existe y está activo, no lo reinicia. Devuelve True si es nuevo."""
        with self.tx() as c:
            fila = c.execute("SELECT estado FROM reportes WHERE id=?", (datos["id"],)).fetchone()
            if fila and fila["estado"] != "DESACTIVADO":
                return False
            c.execute(
                "INSERT OR REPLACE INTO reportes(id, curp, datos, estado, es_prueba, marca_continua, creado, actualizado)"
                " VALUES (?,?,?,'PENDIENTE',?,NULL,?,?)",
                (datos["id"], datos["curp"], json.dumps(datos, ensure_ascii=False), int(es_prueba), ahora(), ahora()),
            )
            return True

    def desactivar(self, reporte_id: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE reportes SET estado='DESACTIVADO', actualizado=? WHERE id=? AND estado!='DESACTIVADO'",
                (ahora(), reporte_id),
            )
            # Lo pendiente de enviar de un caso cerrado ya no debe salir
            c.execute(
                "UPDATE envios SET estado='CANCELADO', actualizado=? WHERE reporte_id=? AND estado='PENDIENTE'",
                (ahora(), reporte_id),
            )
            return cur.rowcount > 0

    def reportes_en(self, estado: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM reportes WHERE estado=? AND es_prueba=0 ORDER BY creado", (estado,)
            ).fetchall()

    def reporte(self, reporte_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM reportes WHERE id=?", (reporte_id,)).fetchone()

    def ids_activos(self) -> set[str]:
        with self._lock:
            return {
                r["id"]
                for r in self._conn.execute("SELECT id FROM reportes WHERE estado!='DESACTIVADO' AND es_prueba=0")
            }

    def cambiar_estado(self, reporte_id: str, estado: str, marca_continua: str | None = None):
        with self.tx() as c:
            c.execute(
                "UPDATE reportes SET estado=?, marca_continua=COALESCE(?, marca_continua), actualizado=?"
                " WHERE id=? AND estado!='DESACTIVADO'",
                (estado, marca_continua, ahora(), reporte_id),
            )

    def actualizar_marca(self, reporte_id: str, marca: str):
        with self.tx() as c:
            c.execute(
                "UPDATE reportes SET marca_continua=?, actualizado=? WHERE id=? AND estado!='DESACTIVADO'",
                (marca, ahora(), reporte_id),
            )

    # ---------- outbox ----------

    @staticmethod
    def huella(*partes: str) -> str:
        return hashlib.sha256("|".join(partes).encode()).hexdigest()

    def encolar(self, huella: str, reporte_id: str, endpoint: str, payload: dict) -> bool:
        """False si ese envío ya existía (no se reporta dos veces lo mismo) o si el caso ya se desactivó."""
        with self.tx() as c:
            fila = c.execute("SELECT estado FROM reportes WHERE id=?", (reporte_id,)).fetchone()
            if not fila or fila["estado"] == "DESACTIVADO":
                return False  # el caso se cerró mientras buscábamos: no se notifica
            cur = c.execute(
                "INSERT OR IGNORE INTO envios(huella, reporte_id, endpoint, payload, estado, creado, actualizado)"
                " VALUES (?,?,?,?,'PENDIENTE',?,?)",
                (huella, reporte_id, endpoint, json.dumps(payload, ensure_ascii=False), ahora(), ahora()),
            )
            return cur.rowcount > 0

    def pendientes(self, limite: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM envios WHERE estado='PENDIENTE' ORDER BY rowid LIMIT ?", (limite,)
            ).fetchall()

    def marcar_envio(self, huella: str, estado: str, error: str | None = None):
        with self.tx() as c:
            c.execute(
                "UPDATE envios SET estado=?, intentos=intentos+1, ultimo_error=?, actualizado=? WHERE huella=?",
                (estado, error, ahora(), huella),
            )

    # ---------- bitácora ----------

    def log(self, tipo: str, operacion: str, reporte_id: str | None = None, resultado: str = "", detalle: str = ""):
        with self.tx() as c:
            c.execute(
                "INSERT INTO bitacora(fecha, tipo, operacion, reporte_id, resultado, detalle) VALUES (?,?,?,?,?,?)",
                (ahora(), tipo, operacion, reporte_id, resultado, detalle[:500]),
            )
