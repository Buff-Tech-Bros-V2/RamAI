from django import forms

from apps.skus.models import DecisionConfig, SKU


class StyledModelForm(forms.ModelForm):
    """Apply the RamAI form styles without repeating widget declarations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css_class = "ram-select" if isinstance(field.widget, forms.Select) else "ram-input"
            field.widget.attrs["class"] = css_class


class SKUForm(StyledModelForm):
    class Meta:
        model = SKU
        fields = ("sku_id", "name", "product_category", "shop_id", "demo_scenario")
        labels = {
            "sku_id": "Kode SKU",
            "name": "Nama produk",
            "product_category": "Kategori",
            "shop_id": "Kode toko",
            "demo_scenario": "Catatan skenario",
        }
        help_texts = {
            "sku_id": "Kode unik, misalnya SKU-005.",
            "demo_scenario": "Opsional. Gunakan untuk mencatat konteks demo atau kondisi produk.",
        }


class DecisionConfigForm(StyledModelForm):
    class Meta:
        model = DecisionConfig
        fields = (
            "operation_mode",
            "constraint_profile",
            "unit_selling_price",
            "unit_variable_cost",
            "production_minutes_per_unit",
            "material_per_unit",
            "supplier_lead_time_hours",
            "minimum_commitment",
            "shelf_life_hours",
            "salvage_value_per_unit",
            "daily_capacity_minutes",
            "working_capital_limit",
            "commitment_deadline",
        )
        labels = {
            "operation_mode": "Cara pemenuhan stok",
            "constraint_profile": "Profil batas operasional",
            "unit_selling_price": "Harga jual per unit",
            "unit_variable_cost": "Biaya per unit",
            "production_minutes_per_unit": "Waktu produksi per unit",
            "material_per_unit": "Material per unit",
            "supplier_lead_time_hours": "Lead time supplier",
            "minimum_commitment": "Minimum produksi/pemesanan",
            "shelf_life_hours": "Masa simpan",
            "salvage_value_per_unit": "Nilai sisa per unit",
            "daily_capacity_minutes": "Kapasitas harian",
            "working_capital_limit": "Batas modal kerja",
            "commitment_deadline": "Batas waktu keputusan",
        }
        widgets = {
            "commitment_deadline": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["commitment_deadline"].input_formats = ["%Y-%m-%dT%H:%M"]
