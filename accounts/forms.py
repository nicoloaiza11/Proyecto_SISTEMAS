from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from .models import CustomUser


class LoginForm(forms.Form):
    """
    Formulario de autenticación adaptado para SOLTAT:
    Permite ingresar mediante DNI/CUIT o nombre de usuario registrado.
    """
    identificador = forms.CharField(
        label='DNI / CUIT o Usuario',
        max_length=50,
        widget=forms.TextInput(
            attrs={
                'class': 'form-control form-control-lg',
                'placeholder': 'Ej. 20-34567890-1 o 34567890',
                'autofocus': True,
                'autocomplete': 'username',
            }
        ),
    )
    password = forms.CharField(
        label='Contraseña',
        widget=forms.PasswordInput(
            attrs={
                'class': 'form-control form-control-lg',
                'placeholder': '••••••••',
                'autocomplete': 'current-password',
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        identificador = cleaned_data.get('identificador', '').strip()
        password = cleaned_data.get('password')

        if identificador and password:
            # Buscar usuario por cuit_dni o username
            user_candidate = CustomUser.objects.filter(cuit_dni__iexact=identificador).first()
            if not user_candidate:
                user_candidate = CustomUser.objects.filter(username__iexact=identificador).first()

            if user_candidate:
                user = authenticate(username=user_candidate.username, password=password)
                if user is not None:
                    if not user.is_active:
                        raise forms.ValidationError('Esta cuenta ha sido desactivada.')
                    cleaned_data['user'] = user
                    return cleaned_data
            
            raise forms.ValidationError('DNI/CUIT o contraseña incorrectos. Verifique sus datos e intente nuevamente.')

        return cleaned_data


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ('username', 'cuit_dni', 'email', 'razon_social', 'rol', 'empresa_asociada', 'telefono')


class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = CustomUser
        fields = ('username', 'cuit_dni', 'email', 'razon_social', 'rol', 'empresa_asociada', 'telefono', 'is_active', 'is_staff')

