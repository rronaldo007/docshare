from django import forms

from .models import Document, Folder


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
