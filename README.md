# Integración con la Plataforma Única de Identidad (PUI)

Esqueleto funcional del servicio que una **institución diversa** debe operar según el
*Manual Técnico de la Solución Tecnológica para Instituciones Diversas* (DOF, 23/01/2026, v1.0).

## Qué hace

```
 PUI ──/login, /activar-reporte, /activar-reporte-prueba, /desactivar-reporte──▶  API (app/main.py)
                                                                              │ guarda y responde 200 de inmediato
                                                                              ▼
                                                               SQLite: reportes · envios · bitacora
                                                                              │
                     worker de fondo (cada 15 s) ◀────────────────────────────┘
                       1. resync     GET /reportes de la PUI (recupera casos perdidos)
                       2. fases 1-2  datos básicos + histórico ≤ 12 años  ──▶ FuenteDatos (TU core)
                       3. fase 3     cambios desde el último corte (en lote)  ──▶ FuenteDatos
                       4. despachar  outbox ──/notificar-coincidencia, /busqueda-finalizada──▶ PUI
```

| Archivo | Responsabilidad |
|---|---|
| `app/main.py` | Endpoints de la sección 8, JWT, rate limit, anti fuerza bruta, encabezados de seguridad, errores sin detalles internos, worker |
| `app/busqueda.py` | Motor de las 3 fases, saneamiento de texto a las regex del manual, outbox con reintentos, resincronización |
| `app/fuente.py` | **Contrato con tus sistemas.** Es lo único que tienes que implementar |
| `app/pui_client.py` | Cliente de la sección 7 (token renovado al 80 %, reintento en 401) |
| `app/schemas.py` | Validaciones exactas del manual |
| `app/cifrado.py` | AES-256-GCM para biométricos (ver pendientes) |
| `app/curp.py` | Anexo 5: CURP → entidad de nacimiento |
| `app/db.py` | Persistencia, deduplicación y bitácora de auditoría |

## Decisiones de diseño

- **Responder rápido, procesar después.** `/activar-reporte` sólo guarda y contesta; la búsqueda corre en el worker. Así una consulta lenta a tu core nunca le provoca un 504 a la PUI.
- **Outbox.** Toda notificación se guarda antes de enviarse. Si la PUI se cae, se reintenta en orden (las coincidencias de la fase 2 siempre salen antes de `/busqueda-finalizada`).
- **Sin duplicados.** Cada coincidencia tiene una huella (id + fase + evento); un evento sólo se vuelve a reportar si se modifica.
- **Idempotencia.** Recibir dos veces el mismo `/activar-reporte` no reinicia la búsqueda.
- **Caso cerrado = silencio.** Tras `/desactivar-reporte` se cancelan los envíos pendientes y el caso sale de la fase 3.
- **Minimización.** Biométricos sólo en la fase 1; la bitácora guarda el id del caso, no la CURP ni los datos personales.

## Cómo correrlo

```bash
uv sync
uv run pytest -q
```

```bash
export PUI_INSTITUCION_RFC=ABC010101AB1        # RFC con homoclave (Anexo 2)
export PUI_BASE_URL=https://<url-que-entregue-la-PUI>
export PUI_CLAVE=<clave del área técnica de la PUI>
export PUI_ENTRADA_CLAVE='<16-20 chars, mayúscula, número y especial>'   # la que usará la PUI contigo
export JWT_SECRETO=<32+ caracteres aleatorios>
export PUI_CLAVE_BIOMETRICOS=<opcional>
uv run uvicorn app.main:app_desde_entorno --factory --port 8443 --no-server-header
```

En producción, pon TLS 1.2+ (en uvicorn o en el balanceador), corre **una sola instancia del worker** y cambia SQLite por tu base corporativa.

## Lo que falta para producción

1. **Implementar `FuenteDatos`** contra tu core (`app/fuente.py`), con índices por CURP y por fecha de modificación.
2. **Definir el catálogo de eventos**: qué operaciones son una señal de vida y cómo se describen (`tipo_evento`, sucursal, dirección).
3. **Confirmar con RENAPO/ATDT**:
   - URLs reales de sandbox y producción, y cómo se entregan las credenciales.
   - Cómo se deriva la llave AES desde la "contraseña de biométricos" y el formato de salida (`app/cifrado.py` asume SHA-256, nonce de 12 bytes y salida `base64(nonce‖cifrado‖tag)`).
   - En qué consiste el "firmado/validado del JSON de respuesta" (el manual lo menciona pero no lo especifica).
   - Si `GET /reportes` lista sólo los casos activos (para permitir desactivaciones automáticas en el resync).
4. **Infraestructura**: dominio con certificado, IP fija, WAF o rate limit compartido si hay más de una instancia, monitoreo y alertas, y MFA en los accesos administrativos.
5. **Seguridad**: SAST + DAST + SCA sin hallazgos de ninguna severidad, sobre el ambiente productivo.
6. **Legal**: aviso de privacidad, base legal para la transferencia y política de retención de la bitácora.
