from django.contrib import admin

from .models import Document, Folder, ShareLink

admin.site.register(Folder)
admin.site.register(Document)
admin.site.register(ShareLink)
