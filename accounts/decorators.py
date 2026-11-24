from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def tipo_requerido(*tipos):
    """
    Restringe una vista a ciertos tipos de usuario ('productor', 'recorredor',
    'operadoracopio', 'empresaacopio'). Pasa a la vista el perfil concreto
    del usuario como `request.perfil`.
    """
    def decorador(vista):
        @wraps(vista)
        def envoltura(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect('login')
            if request.user.tipo not in tipos:
                messages.warning(request, 'Su usuario no tiene acceso a esa sección.')
                return redirect('login_redirect')
            request.perfil = request.user.perfil
            return vista(request, *args, **kwargs)
        return envoltura
    return decorador


productor_required = tipo_requerido('productor')
recorredor_required = tipo_requerido('recorredor')
operador_required = tipo_requerido('operadoracopio')
empresa_required = tipo_requerido('empresaacopio')
