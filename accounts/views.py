from functools import wraps
from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .forms import LoginForm
from .models import CustomUser


def productor_required(view_func):
    """
    Decorador para restringir acceso exclusivamente a usuarios con rol 'productor'
    o administradores.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if request.user.rol == CustomUser.ROL_PRODUCTOR or request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        messages.warning(
            request,
            "Su perfil no tiene permisos de Productor. Ha sido redirigido al panel correspondiente."
        )
        return redirect('turnos:panel_empresa')
    return _wrapped_view


def empresa_required(view_func):
    """
    Decorador para restringir acceso exclusivamente a usuarios con rol 'empresa'
    o administradores.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if request.user.rol == CustomUser.ROL_EMPRESA or request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        messages.warning(
            request,
            "Su perfil no tiene permisos de Empresa de Acopio. Ha sido redirigido al portal de turnos."
        )
        return redirect('turnos:solicitar_turno')
    return _wrapped_view


def login_view(request):
    """
    Vista de autenticación personalizada SOLTAT.
    Si el usuario ya está autenticado, lo redirige según su rol.
    """
    if request.user.is_authenticated:
        return redirect('login_redirect')

    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            user = form.cleaned_data['user']
            login(request, user)
            messages.success(
                request,
                f"Bienvenido/a {user.razon_social or user.get_full_name() or user.username} al sistema SOLTAT."
            )
            return redirect('login_redirect')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {'form': form})


@login_required
def login_redirect_view(request):
    """
    Redirige al usuario a la vista correspondiente según su rol:
    - 'productor' -> 'solicitar-turno'
    - 'empresa' -> 'panel-empresa'
    """
    if request.user.rol == CustomUser.ROL_PRODUCTOR:
        return redirect('turnos:solicitar_turno')
    elif request.user.rol == CustomUser.ROL_EMPRESA:
        return redirect('turnos:panel_empresa')
    elif request.user.is_superuser:
        # Administrador general: acceso prioritario al panel de acopio
        return redirect('turnos:panel_empresa')
    
    return redirect('turnos:solicitar_turno')


def logout_view(request):
    """
    Cierra la sesión y redirige al login con confirmación visual.
    """
    logout(request)
    messages.info(request, "Ha cerrado su sesión correctamente.")
    return redirect('login')

