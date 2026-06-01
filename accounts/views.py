from django import forms
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render

# Usernames reserved for internal use and never registrable. "anonymous" owns
# all no-account uploads (files.views._anonymous_user); letting someone claim it
# would hand them every anonymously-uploaded file.
RESERVED_USERNAMES = {"anonymous"}


class SignupForm(UserCreationForm):
    def clean_username(self):
        username = self.cleaned_data["username"]
        if username.strip().lower() in RESERVED_USERNAMES:
            raise forms.ValidationError("This username is reserved.")
        return username


def signup(request):
    if request.user.is_authenticated:
        return redirect("browse")
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("browse")
    else:
        form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form})
