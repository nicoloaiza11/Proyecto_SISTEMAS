from datetime import date
from django import forms
from .models import Turno, EmpresaAcopio


class SolicitudTurnoForm(forms.ModelForm):
    """
    Formulario del productor tabacalero para solicitar turnos de entrega.
    """
    class Meta:
        model = Turno
        fields = [
            'empresa',
            'kilos_estimados',
            'patente_vehiculo',
            'fecha_entrega',
            'franja_horaria',
            'observaciones',
        ]
        widgets = {
            'empresa': forms.Select(attrs={
                'class': 'form-select form-select-lg',
                'id': 'id_empresa',
            }),
            'kilos_estimados': forms.NumberInput(attrs={
                'class': 'form-control form-control-lg',
                'placeholder': 'Ej. 12000',
                'min': '100',
                'step': '50',
                'id': 'id_kilos_estimados',
            }),
            'patente_vehiculo': forms.TextInput(attrs={
                'class': 'form-control form-control-lg text-uppercase',
                'placeholder': 'Ej. AE123CD o AB123',
                'id': 'id_patente_vehiculo',
            }),
            'fecha_entrega': forms.DateInput(attrs={
                'class': 'form-control form-control-lg',
                'type': 'date',
                'id': 'id_fecha_entrega',
            }),
            'franja_horaria': forms.Select(attrs={
                'class': 'form-select form-select-lg',
                'id': 'id_franja_horaria',
            }),
            'observaciones': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 2,
                'placeholder': 'Observaciones opcionales (tipo de tabaco Burley/Virginia, tipo de bultos, etc.)',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Solo mostrar empresas activas
        self.fields['empresa'].queryset = EmpresaAcopio.objects.filter(activa=True)
        self.fields['empresa'].empty_label = "Seleccione la Empresa Compradora / Acopio"
        
        # Establecer fecha mínima como hoy
        hoy_str = date.today().strftime('%Y-%m-%d')
        self.fields['fecha_entrega'].widget.attrs['min'] = hoy_str

    def clean_fecha_entrega(self):
        fecha = self.cleaned_data.get('fecha_entrega')
        if fecha and fecha < date.today():
            raise forms.ValidationError("La fecha de entrega no puede ser anterior al día de hoy.")
        return fecha

    def clean_kilos_estimados(self):
        kilos = self.cleaned_data.get('kilos_estimados')
        if kilos and kilos <= 0:
            raise forms.ValidationError("La cantidad de kilos estimados debe ser superior a 0.")
        if kilos and kilos > 250000:
            raise forms.ValidationError("La cantidad declarada excede la capacidad total de un centro de acopio.")
        return kilos

