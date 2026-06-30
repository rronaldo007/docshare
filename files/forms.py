from django import forms

from .models import Document, EmailSettings, Folder


class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Folder name"}),
        }


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["file"]


class ShareForm(forms.Form):
    expires_in_days = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=365,
        help_text="Leave blank for a link that never expires.",
    )
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput,
        help_text="Leave blank for a public link.",
    )
    email = forms.EmailField(
        required=False,
        help_text="Optionally email the link to this address.",
    )


class EmailSettingsForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="For Gmail use a 16-character App Password. Leave blank to keep the current one.",
    )

    class Meta:
        model = EmailSettings
        fields = ["enabled", "host", "port", "username", "password", "use_tls", "from_email"]
        widgets = {
            "host": forms.TextInput(attrs={"placeholder": "smtp.gmail.com"}),
            "username": forms.TextInput(attrs={"placeholder": "you@gmail.com"}),
            "from_email": forms.TextInput(attrs={"placeholder": "you@gmail.com"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        text_cls = (
            "w-full border border-slate-200 rounded px-3 py-2 text-sm "
            "focus:outline-none focus:ring-1 focus:ring-indigo-400"
        )
        for name in ("host", "port", "username", "password", "from_email"):
            self.fields[name].widget.attrs.setdefault("class", text_cls)
        for name in ("enabled", "use_tls"):
            self.fields[name].widget.attrs.setdefault("class", "h-4 w-4")
