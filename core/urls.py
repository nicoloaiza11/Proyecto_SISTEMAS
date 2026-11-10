"""
URL configuration for SOLTAT project.
"""
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Cuentas y autenticación
    path('', include('accounts.urls')),
    # Turnos y Acopio
    path('', include('turnos.urls')),
]

