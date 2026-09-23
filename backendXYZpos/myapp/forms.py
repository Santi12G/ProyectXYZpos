from django import forms
from .models import Producto


class ProductoForm(forms.ModelForm):
    # Campo oculto para guardar el stock original y compararlo al editar
    stock_original = forms.IntegerField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = Producto
        # Solo ponemos los campos que existen de verdad en models.py
        fields = ('sku', 'nombre', 'categoria', 'precio', 'stock', 'disponible')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Si el producto ya existe, cargamos su stock actual; si no, es 0
        if self.instance.pk:
            self.fields['stock_original'].initial = self.instance.stock
        else:
            self.fields['stock_original'].initial = 0

        # Estilo visual para todos los campos del formulario
        for field_name in self.fields:
            self.fields[field_name].widget.attrs['class'] = 'bg-slate-900 border border-slate-700 rounded p-2 text-white w-full'
        
        # Al checkbox de 'disponible' le quitamos el ancho completo
        if 'disponible' in self.fields:
            self.fields['disponible'].widget.attrs['class'] = 'w-auto'

    def clean(self):
        cleaned_data = super().clean()
        
        # Validamos que no se pierda el stock original al editar
        if self.instance.pk and cleaned_data.get('stock_original') is None:
            raise forms.ValidationError('Recarga el formulario antes de guardar el inventario.')
            
        return cleaned_data

    def save(self, commit=True, user=None):
        # Creamos el objeto producto sin guardarlo todavía
        producto = super().save(commit=False)
        
        if commit:
            # Guardamos el producto de forma normal (sin argumentos extra que rompen Django)
            producto.save()
            self.save_m2m()
            
        return producto