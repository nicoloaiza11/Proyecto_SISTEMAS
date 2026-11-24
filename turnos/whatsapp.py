"""
Avisos por WhatsApp mediante enlaces wa.me.

No se usa ninguna API paga: el sistema arma el mensaje y un enlace que abre
WhatsApp con el número y el texto ya cargados; el usuario solo toca Enviar.
"""
import re
from urllib.parse import quote


def normalizar_telefono(telefono: str) -> str:
    """Deja solo los dígitos (formato internacional, sin '+')."""
    return re.sub(r'\D', '', telefono or '')


def armar_aviso(destinatario, mensaje: str) -> dict:
    """
    Arma un aviso para un usuario. Si no tiene teléfono cargado, el aviso
    se devuelve sin enlace para que la pantalla lo indique.
    """
    telefono = normalizar_telefono(getattr(destinatario, 'telefono', ''))
    return {
        'destinatario': destinatario.get_nombre_completo(),
        'telefono': destinatario.telefono,
        'mensaje': mensaje,
        'url': f'https://wa.me/{telefono}?text={quote(mensaje)}' if telefono else None,
    }
