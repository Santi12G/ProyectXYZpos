from django import forms
from .models import Producto


class ProductoForm(forms.ModelForm):
    stock_original = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['stock_original'].initial = self.instance.stock if self.instance.pk else 0
        for campo in self.fields.values():
            campo.widget.attrs['class'] = 'bg-slate-900 border border-slate-700 rounded p-2 text-white w-full'
        self.fields['disponible'].widget.attrs['class'] = 'w-auto'

    class Meta:
        model = Producto
        fields = ('image_url', 'sku', 'barcode', 'nombre', 'description', 'categoria', 'precio', 'cost_price', 'stock', 'min_stock', 'disponible')
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def clean_barcode(self):
        return self.cleaned_data.get('barcode') or None

    def clean(self):
        datos = super().clean()
        if self.instance.pk and datos.get('stock_original') is None:
            raise forms.ValidationError('Recarga el formulario antes de guardar el inventario.')
        return datos

    def save(self, commit=True, *, user=None):
        producto = super().save(commit=False)
        if commit:
            producto.save(user=user, expected_stock=self.cleaned_data.get('stock_original'))
            self.save_m2m()
        return producto
