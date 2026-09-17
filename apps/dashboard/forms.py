from django import forms

from apps.skus.models import (
    CONSTRAINT_PROFILE_PRESETS,
    DecisionConfig,
    OperationMode,
    SKU,
)

# Which decision fields each operation mode actually uses. The form hides the
# other mode's fields (client side) and refuses to require them (server side),
# so a seller only ever fills the constraints that bind their own workflow.
MODE_FIELDS = {
    OperationMode.PRODUCTION: ["production_minutes_per_unit", "daily_capacity_minutes"],
    OperationMode.REPLENISHMENT: ["supplier_lead_time_hours"],
}

# Shape-of-the-problem choices, shown above the numbers: they decide which
# fields appear and what the presets fill in.
TEMPLATE_FIELDS = ["constraint_profile", "operation_mode"]

# Fields the presets or model defaults already fill. They stay editable but
# live behind a disclosure so the main grid is only the numbers a seller has
# to look up.
ADVANCED_FIELDS = [
    "minimum_commitment",
    "shelf_life_hours",
    "salvage_value_per_unit",
    "material_per_unit",
]


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
            "decision_window_hours",
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
            "decision_window_hours": "Keputusan harus diambil dalam",
        }
        help_texts = {
            "operation_mode": "Menentukan batasan mana yang perlu diisi di bawah.",
            "constraint_profile": "Mengisi otomatis masa simpan, minimum, dan tenggat.",
            "decision_window_hours": "Jam dari sekarang, bukan tanggal tetap.",
            "material_per_unit": "Opsional. Dipakai untuk menghitung kebutuhan material.",
        }

    #: Fields shown only when the matching operation mode is selected.
    mode_only_fields = MODE_FIELDS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Capacity is required *together with* production minutes or not at
        # all, so neither is required at the field level -- clean() decides
        # based on the chosen mode.
        for names in MODE_FIELDS.values():
            for name in names:
                self.fields[name].required = False

    @property
    def template_fields(self):
        """Profile + mode: the two choices that reshape the rest of the form."""
        return [self[name] for name in TEMPLATE_FIELDS]

    @property
    def advanced_fields(self):
        return [self[name] for name in ADVANCED_FIELDS]

    @property
    def primary_fields(self):
        grouped = set(TEMPLATE_FIELDS) | set(ADVANCED_FIELDS)
        return [field for field in self if field.name not in grouped]

    @property
    def advanced_has_errors(self) -> bool:
        """Keep the disclosure open when something inside it needs fixing."""
        return any(self[name].errors for name in ADVANCED_FIELDS)

    @property
    def form_config(self) -> dict:
        """Profile presets + per-mode field lists, consumed by the form's JS."""
        return {
            # On an edit form the stored values win: switching template must
            # never silently overwrite a product's real numbers.
            "isEdit": self.instance.pk is not None,
            "presets": {
                str(profile): {str(k): v for k, v in preset.items()}
                for profile, preset in CONSTRAINT_PROFILE_PRESETS.items()
            },
            "modeFields": {
                str(mode): [f"id_{name}" for name in names]
                for mode, names in MODE_FIELDS.items()
            },
        }

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("operation_mode")

        if mode == OperationMode.PRODUCTION:
            # Daily capacity is meaningless without minutes-per-unit: the
            # engine cannot convert it into a unit ceiling, so it would be
            # silently ignored. Demand both or reject.
            for name in MODE_FIELDS[OperationMode.PRODUCTION]:
                if cleaned.get(name) in (None, ""):
                    self.add_error(
                        name, "Wajib diisi untuk mode produksi sendiri."
                    )
            cleaned["supplier_lead_time_hours"] = None
        elif mode == OperationMode.REPLENISHMENT:
            if cleaned.get("supplier_lead_time_hours") in (None, ""):
                self.add_error(
                    "supplier_lead_time_hours",
                    "Wajib diisi untuk mode pesan ke supplier.",
                )
            cleaned["production_minutes_per_unit"] = None
            cleaned["daily_capacity_minutes"] = None

        return cleaned
