from django import forms
from django.utils import timezone

from accounts.models import Productor
from .choices import CorteTabaco

INPUT = {'class': 'form-control form-control-lg'}
SELECT = {'class': 'form-select form-select-lg'}


class DatosTurnoForm(forms.Form):
    """Campos comunes a la asignación (recorredor) y turnos con fecha."""
    kilos_estimados = forms.IntegerField(
        label='Kilos estimados', min_value=1,
        widget=forms.NumberInput(attrs={**INPUT, 'placeholder': 'Ej. 12000', 'step': '50'}),
    )
    corte_tabaco = forms.ChoiceField(
        label='Corte de tabaco', choices=CorteTabaco.choices, widget=forms.Select(attrs=SELECT),
    )
    patente_vehiculo = forms.CharField(
        label='Patente del vehículo', max_length=20,
        widget=forms.TextInput(attrs={**INPUT, 'class': INPUT['class'] + ' text-uppercase', 'placeholder': 'Ej. AE123CD'}),
    )
    fecha_entrega = forms.DateField(
        label='Fecha de entrega',
        widget=forms.DateInput(attrs={**INPUT, 'type': 'date'}, format='%Y-%m-%d'),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['fecha_entrega'].widget.attrs['min'] = timezone.localdate().isoformat()

    def clean_fecha_entrega(self):
        fecha = self.cleaned_data['fecha_entrega']
        if fecha < timezone.localdate():
            raise forms.ValidationError('La fecha de entrega no puede ser anterior a hoy.')
        return fecha


class SolicitudTurnoForm(forms.Form):
    """El productor elige empresa, kilos, corte y patente; la fecha y franja las asigna el sistema al confirmar."""
    empresa = forms.ModelChoiceField(
        label='Empresa compradora', queryset=None, empty_label='Seleccione la empresa',
        widget=forms.Select(attrs=SELECT),
    )
    kilos_estimados = forms.IntegerField(
        label='Kilos estimados', min_value=1,
        widget=forms.NumberInput(attrs={**INPUT, 'placeholder': 'Ej. 12000', 'step': '50'}),
    )
    corte_tabaco = forms.ChoiceField(
        label='Corte de tabaco', choices=CorteTabaco.choices, widget=forms.Select(attrs=SELECT),
    )
    patente_vehiculo = forms.CharField(
        label='Patente del vehículo', max_length=20,
        widget=forms.TextInput(attrs={**INPUT, 'class': INPUT['class'] + ' text-uppercase', 'placeholder': 'Ej. AE123CD'}),
    )
    field_order = ['empresa', 'kilos_estimados', 'corte_tabaco', 'patente_vehiculo']

    def __init__(self, *args, empresas, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['empresa'].queryset = empresas


class AsignarTurnoForm(DatosTurnoForm):
    """El recorredor asigna un turno a un productor con cupo en su empresa."""
    productor = forms.ModelChoiceField(
        label='Productor', queryset=Productor.objects.none(), empty_label='Seleccione el productor',
        widget=forms.Select(attrs=SELECT),
    )
    field_order = ['productor', 'fecha_entrega', 'kilos_estimados', 'corte_tabaco', 'patente_vehiculo']

    def __init__(self, *args, empresa, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['productor'].queryset = (
            Productor.objects.filter(cupos__recorredor__empresa_asignada=empresa).distinct()
        )
        self.fields['productor'].label_from_instance = lambda p: f'{p.get_nombre_completo()} · {p.cuit_dni}'


class CancelacionForm(forms.Form):
    mensaje = forms.CharField(
        label='Motivo de la cancelación', required=False, max_length=500,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3,
                                     'placeholder': 'Explique por qué cancela el turno'}),
    )


class ConvocatoriaForm(forms.Form):
    """EmpresaAcopio.enviar_aviso(corte_tabaco, kilos, mensaje)"""
    corte_tabaco = forms.ChoiceField(label='Corte solicitado', choices=CorteTabaco.choices,
                                     widget=forms.Select(attrs={'class': 'form-select'}))
    kilos = forms.IntegerField(label='Kilos buscados', min_value=1,
                               widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '500'}))
    mensaje = forms.CharField(label='Mensaje', required=False, max_length=500,
                              widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 2,
                                                           'placeholder': 'Opcional'}))
