from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import LoginForm

# Pantalla de inicio de cada tipo de usuario
INICIO_POR_TIPO = {
    'productor': 'turnos:mis_turnos',
    'recorredor': 'turnos:panel_recorredor',
    'operadoracopio': 'turnos:panel_operador',
    'empresaacopio': 'turnos:panel_empresa',
}


def login_view(request):
    if request.user.is_authenticated:
        return redirect('login_redirect')
    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            user = form.cleaned_data['user']
            login(request, user)
            messages.success(request, f'Bienvenido/a {user.get_nombre_completo()} al sistema SOLTAT.')
            return redirect('login_redirect')
    else:
        form = LoginForm()
    return render(request, 'accounts/login.html', {'form': form})


@login_required
def login_redirect_view(request):
    """Lleva a cada usuario a su pantalla según su tipo."""
    destino = INICIO_POR_TIPO.get(request.user.tipo)
    if destino:
        return redirect(destino)
    if request.user.es_administrador:
        return redirect('admin:index')
    logout(request)
    messages.error(request, 'Su usuario no tiene un perfil asignado. Contacte al administrador.')
    return redirect('login')


def logout_view(request):
    logout(request)
    return redirect('login')
