"""Sección 9: biométricos -> base64 -> AES-256-GCM con la clave de biométricos de la institución.

PENDIENTE DE CONFIRMAR CON RENAPO/ATDT: el manual habla de una "contraseña" pero no dice
cómo se deriva la llave de 256 bits, ni el tamaño del nonce, ni el formato de salida.
Aquí se asume: llave = SHA-256(contraseña), nonce de 12 bytes, salida base64(nonce || cifrado || tag).
Ajusta `_derivar_llave` y `cifrar` cuando tengas la especificación oficial.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _derivar_llave(clave: str) -> bytes:
    return hashlib.sha256(clave.encode("utf-8")).digest()


def cifrar(binario: bytes, clave: str) -> str:
    if not clave:
        raise ValueError("No hay clave de biométricos configurada")
    contenido_b64 = base64.b64encode(binario)
    nonce = os.urandom(12)
    cifrado = AESGCM(_derivar_llave(clave)).encrypt(nonce, contenido_b64, None)
    return base64.b64encode(nonce + cifrado).decode("ascii")


def descifrar(texto: str, clave: str) -> bytes:
    """Sólo para pruebas internas."""
    crudo = base64.b64decode(texto)
    contenido_b64 = AESGCM(_derivar_llave(clave)).decrypt(crudo[:12], crudo[12:], None)
    return base64.b64decode(contenido_b64)
