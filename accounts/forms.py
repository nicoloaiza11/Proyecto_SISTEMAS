from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import Usuario


class LoginForm(forms.Form):
    """Ingreso con DNI/CUIT o nombre de usuario."""
    identificador = forms.CharField(
        label='DNI / CUIT o Usuario',
        max_length=50,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Ej. 20345678901',
            'autofocus': True,
            'autocomplete': 'username',
        }),
    )
    password = forms.CharField(
        label='Contraseña',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': '••••••••',
            'autocomplete': 'current-password',
        }),
    )

    def clean(self):
        cleaned_data = super().clean()
        identificador = (cleaned_data.get('identificador') or '').strip()
        password = cleaned_data.get('password')
        if identificador and password:
            candidato = (
                Usuario.objects.filter(cuit_dni__iexact=identificador).first()
                or Usuario.objects.filter(username__iexact=identificador).first()
            )
            if candidato:
                user = authenticate(username=candidato.username, password=password)
                if user is not None:
                    if not user.is_active:
                        raise forms.ValidationError('Esta cuenta está desactivada.')
                    cleaned_data['user'] = user
                    return cleaned_data
            raise forms.ValidationError('DNI/CUIT o contraseña incorrectos.')
        return cleaned_data


def crear_formularios_usuario(modelo, campos_extra=()):
    """Formularios de alta y edición para el panel /admin de cada tipo de usuario."""
    campos = ('username', 'cuit_dni', 'first_name', 'last_name', 'email', 'telefono') + tuple(campos_extra)

    class AltaForm(UserCreationForm):
        class Meta(UserCreationForm.Meta):
            model = modelo
            fields = campos

    class EdicionForm(UserChangeForm):
        class Meta(UserChangeForm.Meta):
            model = modelo
            fields = '__all__'

    return AltaForm, EdicionForm
