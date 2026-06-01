from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.shortcuts import redirect, render

from .forms import ContactForm


def home(request):
    return render(request, "pages/home.html")


def about(request):
    return render(request, "pages/about.html")


def contact(request):
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            send_mail(
                subject=f"DocShare contact from {data['name']}",
                message=f"From: {data['name']} <{data['email']}>\n\n{data['message']}",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[settings.CONTACT_EMAIL],
                fail_silently=False,
            )
            messages.success(request, "Thanks for reaching out. We'll be in touch.")
            return redirect("contact")
    else:
        form = ContactForm()
    return render(request, "pages/contact.html", {"form": form})
