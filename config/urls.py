from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from files import views as files_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    # Public, no-account routes live at the root. Share links MUST stay at
    # /s/<token>/ (see CLAUDE.md); the authed file browser is mounted at /files/.
    path("share-anon/", files_views.anonymous_upload, name="anonymous_upload"),
    path("s/<uuid:token>/", files_views.share_view, name="share_view"),
    path(
        "s/<uuid:token>/doc/<int:doc_id>/",
        files_views.share_download,
        name="share_download",
    ),
    path(
        "s/<uuid:token>/doc/<int:doc_id>/view/",
        files_views.share_preview,
        name="share_preview",
    ),
    path("", include("pages.urls")),
    path("files/", include("files.urls")),
]

# Serve uploaded media through Django ONLY in DEBUG. This raw /media/ route has
# no authentication, ownership scoping, or share-link guards, so it must never
# be exposed in production -- doing so would let anyone fetch any user's private
# file by guessing its path, bypassing the entire access-control model. All
# real file delivery (owner and public) goes through the guarded views in
# files.views (inline_document / download_document / share_preview /
# share_download), which is what the templates reference. In production, serve
# MEDIA_ROOT from outside any web-reachable path.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
