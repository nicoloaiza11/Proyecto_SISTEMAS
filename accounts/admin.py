from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser
from .forms import CustomUserCreationForm, CustomUserChangeForm


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = CustomUser
    list_display = ('username', 'cuit_dni', 'razon_social', 'rol', 'empresa_asociada', 'is_active', 'is_staff')
    list_filter = ('rol', 'is_active', 'is_staff', 'empresa_asociada')
    search_fields = ('username', 'cuit_dni', 'razon_social', 'email')
    ordering = ('cuit_dni',)

    fieldsets = UserAdmin.fieldsets + (
        ('Datos Específicos SOLTAT', {
            'fields': ('cuit_dni', 'rol', 'razon_social', 'telefono', 'empresa_asociada'),
        }),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Datos Específicos SOLTAT', {
            'fields': ('cuit_dni', 'rol', 'razon_social', 'telefono', 'empresa_asociada'),
        }),
    )

