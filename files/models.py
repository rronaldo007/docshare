import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Folder(models.Model):
    name = models.CharField(max_length=255)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="folders",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def breadcrumbs(self):
        """Return folders from root down to this one."""
        chain = []
        node = self
        while node is not None:
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    def get_absolute_url(self):
        return reverse("browse", args=[self.pk])


# Types we will render in the browser, and how. Raster images and PDF are
# served as their own type; text-like types are served as inert text/plain
# (never their native type) so any markup inside cannot execute. HTML and SVG
# are deliberately ABSENT from both sets: each can run script if served inline,
# so they are always downloaded instead. _serve_file in views.py enforces this.
INLINE_IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
)
TEXT_LIKE_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "text/xml",
        "text/csv",
        "text/markdown",
        "text/tab-separated-values",
    }
)


def upload_path(instance, filename):
    # Insert an unguessable per-file directory so stored paths are not
    # enumerable from the owner id + original filename. Defense in depth: raw
    # media is not web-reachable in production, but this also stops one user's
    # upload from colliding with / overwriting another's same-named file.
    return f"user_{instance.owner_id}/{uuid.uuid4().hex}/{filename}"


class Document(models.Model):
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to=upload_path)
    folder = models.ForeignKey(
        Folder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="documents",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    # Set only for no-account uploads; used to rate-limit anonymous sharing.
    uploader_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def kind(self):
        """Coarse category that picks a preview renderer AND mirrors what can be
        shown safely inline. 'image'/'pdf' render as-is; 'text' renders as inert
        plain text; 'other' (incl. HTML and SVG) is download-only."""
        ct = (self.content_type or "").split(";")[0].strip().lower()
        if ct in INLINE_IMAGE_TYPES:
            return "image"
        if ct == "application/pdf":
            return "pdf"
        # text/* is safe to show as plain text, EXCEPT text/html (executes).
        if ct in TEXT_LIKE_TYPES or (ct.startswith("text/") and ct != "text/html"):
            return "text"
        return "other"

    @property
    def pretty_size(self):
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024


class ShareLink(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    folder = models.ForeignKey(
        Folder,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="share_links",
    )
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="share_links",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="share_links",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    password = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        target = self.folder or self.document
        return f"Share: {target}"

    @property
    def is_expired(self):
        return self.expires_at is not None and timezone.now() > self.expires_at

    @property
    def requires_password(self):
        return bool(self.password)

    def set_password(self, raw_password):
        """Store a hashed password (empty string leaves the link public)."""
        self.password = make_password(raw_password) if raw_password else ""

    def check_password(self, raw_password):
        return bool(raw_password) and check_password(raw_password, self.password)

    @property
    def target(self):
        return self.folder or self.document

    def get_absolute_url(self):
        return reverse("share_view", args=[self.token])
