import mimetypes
import tempfile
import zipfile
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import DocumentForm, FolderForm, ShareForm
from .models import Document, Folder, ShareLink


# Content types we are willing to serve INLINE (rendered in the browser at our
# own origin). Anything else is forced to download as an attachment so an
# uploaded HTML/SVG/JS file can never execute as same-origin script. SVG and
# HTML are deliberately excluded: both can carry script.
INLINE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "application/pdf",
    "text/plain",
}


def _safe_content_type(upload, filename=None):
    """Derive a stored Content-Type WITHOUT trusting the client-supplied header.

    The browser-sent multipart Content-Type is attacker-controlled, so a file
    could be labeled text/html or image/svg+xml and later rendered inline as
    script. We instead guess from the (server-side) filename extension, and only
    accept a client header when it maps to a known-safe inline type. Everything
    else falls back to application/octet-stream and is served as a download.
    """
    name = filename or upload.name or ""
    guessed = mimetypes.guess_type(name)[0]
    if guessed and guessed.lower() in INLINE_CONTENT_TYPES:
        return guessed.lower()
    declared = (getattr(upload, "content_type", "") or "").split(";")[0].strip().lower()
    if declared in INLINE_CONTENT_TYPES:
        return declared
    return guessed or "application/octet-stream"


def _serve_file(doc, *, inline):
    """Serve a document's bytes safely.

    Inline serving is gated by doc.kind (so it always agrees with the preview
    template) and the Content-Type is re-derived server-side, never taken from
    the client-supplied header:
      - image / pdf -> served as their own (allowlisted, inert) type
      - text-like   -> served as text/plain so any HTML/markup inside is shown
                       as inert source and can never execute (XSS-safe)
      - anything else (incl. HTML, SVG, binaries) -> forced download
    X-Content-Type-Options: nosniff is always set so the browser cannot sniff
    the body into something more dangerous than what we declared.
    """
    kind = doc.kind
    if inline and kind == "image":
        content_type = (doc.content_type or "").split(";")[0].strip().lower()
        response = FileResponse(doc.file.open("rb"), content_type=content_type)
    elif inline and kind == "pdf":
        response = FileResponse(doc.file.open("rb"), content_type="application/pdf")
    elif inline and kind == "text":
        response = FileResponse(
            doc.file.open("rb"), content_type="text/plain; charset=utf-8"
        )
    else:
        response = FileResponse(
            doc.file.open("rb"), as_attachment=True, filename=doc.name
        )
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------- Authenticated browsing ----------

@login_required
def browse(request, folder_id=None):
    current = None
    if folder_id is not None:
        current = get_object_or_404(Folder, pk=folder_id, owner=request.user)

    all_folders = list(Folder.objects.filter(owner=request.user))
    by_parent = {}
    for f in all_folders:
        by_parent.setdefault(f.parent_id, []).append(f)

    def _descendant_ids(folder_id):
        """All folder ids nested anywhere beneath folder_id."""
        out = []
        stack = list(by_parent.get(folder_id, []))
        while stack:
            node = stack.pop()
            out.append(node.id)
            stack.extend(by_parent.get(node.id, []))
        return out

    path_label = {f.id: " / ".join(c.name for c in f.breadcrumbs) for f in all_folders}

    folders = [f for f in all_folders if f.parent_id == (current.id if current else None)]
    for folder in folders:
        # Valid move targets exclude the folder itself, anything inside it
        # (would create a cycle), and its current parent (a no-op move).
        blocked = {folder.id, folder.parent_id}
        blocked.update(_descendant_ids(folder.id))
        folder.move_options = sorted(
            ({"id": f.id, "label": path_label[f.id]} for f in all_folders if f.id not in blocked),
            key=lambda o: o["label"],
        )
        folder.can_move_to_root = folder.parent_id is not None

    documents = Document.objects.filter(owner=request.user, folder=current)

    return render(
        request,
        "files/browse.html",
        {
            "current": current,
            "folders": folders,
            "documents": documents,
            "folder_form": FolderForm(),
            "document_form": DocumentForm(),
        },
    )


@login_required
def create_folder(request, folder_id=None):
    if request.method != "POST":
        return redirect("browse")
    parent = None
    if folder_id is not None:
        parent = get_object_or_404(Folder, pk=folder_id, owner=request.user)
    form = FolderForm(request.POST)
    if form.is_valid():
        folder = form.save(commit=False)
        folder.owner = request.user
        folder.parent = parent
        folder.save()
        messages.success(request, f"Folder '{folder.name}' created.")
    return redirect(parent.get_absolute_url() if parent else "browse")


@login_required
def move_folder(request, folder_id):
    if request.method != "POST":
        return redirect("browse")
    folder = get_object_or_404(Folder, pk=folder_id, owner=request.user)

    dest_id = request.POST.get("destination") or ""
    destination = None
    if dest_id:
        destination = get_object_or_404(Folder, pk=dest_id, owner=request.user)
        # Reject moving a folder into itself or one of its own descendants;
        # that would detach a cycle from the tree.
        node = destination
        while node is not None:
            if node.id == folder.id:
                messages.error(request, "You can't move a folder into itself.")
                return redirect(
                    folder.parent.get_absolute_url() if folder.parent else "browse"
                )
            node = node.parent

    folder.parent = destination
    folder.save()
    messages.success(request, f"Moved '{folder.name}'.")
    return redirect(destination.get_absolute_url() if destination else "browse")


@login_required
def upload_document(request, folder_id=None):
    if request.method != "POST":
        return redirect("browse")
    folder = None
    if folder_id is not None:
        folder = get_object_or_404(Folder, pk=folder_id, owner=request.user)
    form = DocumentForm(request.POST, request.FILES)
    if form.is_valid():
        upload = request.FILES["file"]
        doc = form.save(commit=False)
        doc.owner = request.user
        doc.folder = folder
        doc.name = upload.name
        doc.size = upload.size
        doc.content_type = _safe_content_type(upload)
        doc.save()
        messages.success(request, f"Uploaded '{doc.name}'.")
    else:
        messages.error(request, "Upload failed. Pick a file and try again.")
    return redirect(folder.get_absolute_url() if folder else "browse")


def _get_or_create_path(owner, parent, segments):
    """Walk/create a chain of folders under parent, reusing existing ones."""
    node = parent
    for name in segments:
        node, _ = Folder.objects.get_or_create(owner=owner, parent=node, name=name)
    return node


@login_required
def upload_folder(request, folder_id=None):
    """Upload a whole folder, recreating its subfolder structure.

    The browser sends each file's path-within-the-folder in a parallel "paths"
    field (e.g. "Photos/2024/a.jpg"), aligned by position with "files"; we split
    that to rebuild the tree under the current folder. (Django strips directory
    components from the upload filename itself, so the path must travel
    separately.) Path segments are sanitized so a crafted name can never escape
    the user's own folder tree.
    """
    if request.method != "POST":
        return redirect("browse")
    parent = None
    if folder_id is not None:
        parent = get_object_or_404(Folder, pk=folder_id, owner=request.user)

    uploads = request.FILES.getlist("files")
    paths = request.POST.getlist("paths")
    if not uploads:
        messages.error(request, "No folder selected, or it was empty.")
        return redirect(parent.get_absolute_url() if parent else "browse")

    created = 0
    for i, upload in enumerate(uploads):
        rel = paths[i] if i < len(paths) else (upload.name or "")
        rel = rel.replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        if not parts:
            parts = [upload.name] if upload.name else []
        if not parts:
            continue
        *dirs, filename = parts
        folder = _get_or_create_path(request.user, parent, dirs)

        upload.name = filename  # store under the basename, like single upload
        doc = Document(
            owner=request.user,
            folder=folder,
            name=filename,
            size=upload.size,
            file=upload,
        )
        doc.content_type = _safe_content_type(upload, filename=filename)
        doc.save()
        created += 1

    # The browser uploader sends files in batches via XHR; reply with a tiny
    # 204 so it can move to the next batch without downloading a full HTML page
    # (and without a per-batch flash message). Non-JS posts get the normal flow.
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return HttpResponse(status=204)

    messages.success(
        request, f"Uploaded {created} file{'' if created == 1 else 's'}."
    )
    return redirect(parent.get_absolute_url() if parent else "browse")


@login_required
def delete_folder(request, folder_id):
    folder = get_object_or_404(Folder, pk=folder_id, owner=request.user)
    parent = folder.parent
    folder.delete()
    messages.success(request, "Folder deleted.")
    return redirect(parent.get_absolute_url() if parent else "browse")


@login_required
def delete_document(request, doc_id):
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    folder = doc.folder
    doc.file.delete(save=False)
    doc.delete()
    messages.success(request, "File deleted.")
    return redirect(folder.get_absolute_url() if folder else "browse")


@login_required
def preview_document(request, doc_id):
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    return render(request, "files/preview.html", {"doc": doc})


@login_required
def inline_document(request, doc_id):
    """Owner-only inline file serving for the preview page (img/iframe src).

    Replaces the raw /media/ URL so uploads are never reachable without an
    ownership check, and goes through the same safe-type allowlist as public
    previews.
    """
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    return _serve_file(doc, inline=True)


@login_required
def download_document(request, doc_id):
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    return _serve_file(doc, inline=False)


# ---------- Sharing ----------

@login_required
def create_share(request, kind, obj_id):
    if request.method != "POST":
        return redirect("browse")

    if kind == "folder":
        target = get_object_or_404(Folder, pk=obj_id, owner=request.user)
    elif kind == "document":
        target = get_object_or_404(Document, pk=obj_id, owner=request.user)
    else:
        raise Http404

    form = ShareForm(request.POST)
    if not form.is_valid():
        # Do NOT silently mint a public, never-expiring link when the form is
        # invalid (e.g. expires_in_days out of range). Fail closed and report.
        messages.error(
            request, "Could not create share link. Check the expiry value and try again."
        )
        if kind == "folder" and target.parent:
            return redirect(target.parent.get_absolute_url())
        if kind == "document" and target.folder:
            return redirect(target.folder.get_absolute_url())
        return redirect("browse")

    expires_at = None
    days = form.cleaned_data.get("expires_in_days")
    if days:
        expires_at = timezone.now() + timedelta(days=days)
    password = form.cleaned_data.get("password") or ""

    link = ShareLink(created_by=request.user, expires_at=expires_at)
    if password:
        link.set_password(password)
    if kind == "folder":
        link.folder = target
    else:
        link.document = target
    link.save()

    share_url = request.build_absolute_uri(link.get_absolute_url())
    protected = " (password protected)" if link.requires_password else ""
    messages.success(request, f"Share link created{protected}: {share_url}")

    if kind == "folder" and target.parent:
        return redirect(target.parent.get_absolute_url())
    if kind == "document" and target.folder:
        return redirect(target.folder.get_absolute_url())
    return redirect("browse")


@login_required
def my_links(request):
    links = ShareLink.objects.filter(created_by=request.user).order_by("-created_at")
    return render(request, "files/links.html", {"links": links})


@login_required
def revoke_link(request, token):
    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    link.delete()
    messages.success(request, "Link revoked.")
    return redirect("my_links")


# ---------- Anonymous sharing (no account) ----------

ANON_DAILY_LIMIT = 5
ANON_LINK_EXPIRY_DAYS = 7
ANON_USERNAME = "anonymous"


def _client_ip(request):
    # X-Forwarded-For is client-controllable and trivially spoofable, which
    # would defeat the per-IP anonymous-upload rate limit. Only honor it when
    # explicitly told we sit behind a trusted proxy that overwrites it; in that
    # case the real client is the LAST (rightmost) hop the proxy appended.
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def _anonymous_user():
    """A single reserved account that owns all no-account uploads.

    Keeps the non-null owner invariant intact; the account can never log in.
    """
    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=ANON_USERNAME,
        defaults={"is_active": False},
    )
    # Defensive: never reuse an account that someone managed to make loginable
    # (e.g. by registering the reserved username before it existed) as the
    # owner of anonymous uploads. Force it inert every time.
    if created or user.is_active or user.has_usable_password():
        user.is_active = False
        user.set_unusable_password()
        user.save(update_fields=["is_active", "password"])
    return user


def anonymous_upload(request):
    """Upload one file without an account and get a public share link.

    Rate limited to ANON_DAILY_LIMIT uploads per client IP per day; the link
    auto-expires so ownerless links self-clean.
    """
    anon = _anonymous_user()
    ip = _client_ip(request)
    today_count = Document.objects.filter(
        owner=anon, uploader_ip=ip, created_at__date=timezone.localdate()
    ).count()

    if request.method == "POST":
        if today_count >= ANON_DAILY_LIMIT:
            messages.error(
                request,
                f"Daily upload limit reached ({ANON_DAILY_LIMIT} per day). "
                "Try again tomorrow.",
            )
            return redirect("anonymous_upload")

        form = DocumentForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Upload failed. Pick a file and try again.")
            return redirect("anonymous_upload")

        upload = request.FILES["file"]
        max_bytes = getattr(settings, "ANON_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
        if upload.size > max_bytes:
            messages.error(
                request,
                f"File too large. Anonymous uploads are limited to "
                f"{max_bytes // (1024 * 1024)} MB.",
            )
            return redirect("anonymous_upload")

        doc = form.save(commit=False)
        doc.owner = anon
        doc.folder = None
        doc.name = upload.name
        doc.size = upload.size
        doc.content_type = _safe_content_type(upload)
        doc.uploader_ip = ip
        doc.save()

        link = ShareLink(
            created_by=anon,
            document=doc,
            expires_at=timezone.now() + timedelta(days=ANON_LINK_EXPIRY_DAYS),
        )
        link.save()

        share_url = request.build_absolute_uri(link.get_absolute_url())
        messages.success(request, f"Your file is shared: {share_url}")
        return redirect("share_view", token=link.token)

    return render(
        request,
        "files/anonymous_upload.html",
        {
            "form": DocumentForm(),
            "remaining": max(0, ANON_DAILY_LIMIT - today_count),
            "limit": ANON_DAILY_LIMIT,
            "expiry_days": ANON_LINK_EXPIRY_DAYS,
        },
    )


# ---------- Public (no login) ----------

# Brute-force protection for password-protected links (per token + client IP).
PASSWORD_MAX_ATTEMPTS = 10
PASSWORD_LOCKOUT_SECONDS = 300


def _valid_link(token):
    link = get_object_or_404(ShareLink, token=token)
    if link.is_expired:
        raise Http404("This link has expired.")
    return link


def _is_unlocked(request, link):
    """A public link is always unlocked; a protected one needs a session unlock."""
    if not link.requires_password:
        return True
    unlocked = request.session.get("unlocked_share_links", [])
    return str(link.token) in unlocked


MAX_REMEMBERED_UNLOCKS = 50


def _unlock(request, link):
    unlocked = request.session.get("unlocked_share_links", [])
    token = str(link.token)
    if token in unlocked:
        return
    unlocked.append(token)
    # Cap the list so a client can't inflate session size by unlocking many
    # links; drop the oldest entries past the cap (FIFO).
    if len(unlocked) > MAX_REMEMBERED_UNLOCKS:
        unlocked = unlocked[-MAX_REMEMBERED_UNLOCKS:]
    request.session["unlocked_share_links"] = unlocked


def share_view(request, token):
    link = _valid_link(token)

    if link.requires_password and not _is_unlocked(request, link):
        if request.method == "POST":
            cache_key = f"share_pw_fail:{token}:{_client_ip(request)}"
            attempts = cache.get(cache_key, 0)
            if attempts >= PASSWORD_MAX_ATTEMPTS:
                messages.error(
                    request,
                    "Too many incorrect attempts. Please wait a few minutes and try again.",
                )
            elif link.check_password(request.POST.get("password", "")):
                cache.delete(cache_key)
                _unlock(request, link)
                return redirect("share_view", token=token)
            else:
                # Throttle brute-force guessing per token+IP.
                cache.set(cache_key, attempts + 1, PASSWORD_LOCKOUT_SECONDS)
                messages.error(request, "Incorrect password.")
        return render(request, "files/share_password.html", {"link": link})

    if link.document:
        return render(
            request,
            "files/share_document.html",
            {"link": link, "doc": link.document},
        )

    folder = link.folder
    subfolders = folder.children.all()
    documents = folder.documents.all()
    # Count every downloadable file (this folder + nested) so we can show a
    # summary and only offer "Download all" when there's something to zip.
    total_files = len(_descendant_documents(folder))
    return render(
        request,
        "files/share_folder.html",
        {
            "link": link,
            "folder": folder,
            "subfolders": subfolders,
            "documents": documents,
            "subfolder_count": subfolders.count(),
            "total_files": total_files,
        },
    )


def _shared_doc_or_404(link, doc_id):
    """Fetch a document only if it is actually reachable through this link.

    Scopes the query to the link's owner and fails closed: a document link
    serves exactly its own document; a folder link serves only files that
    _is_descendant confirms live under the shared folder. This upholds the core
    invariant that a share link never exposes a file outside its target.
    """
    doc = get_object_or_404(Document, pk=doc_id, owner=link.created_by)
    if link.document_id and link.document_id == doc.id:
        return doc
    if link.folder_id and _is_descendant(doc, link.folder):
        return doc
    raise Http404


def share_download(request, token, doc_id):
    link = _valid_link(token)
    if not _is_unlocked(request, link):
        return redirect("share_view", token=token)
    doc = _shared_doc_or_404(link, doc_id)
    return _serve_file(doc, inline=False)


def share_preview(request, token, doc_id):
    link = _valid_link(token)
    if not _is_unlocked(request, link):
        return redirect("share_view", token=token)
    doc = _shared_doc_or_404(link, doc_id)
    return _serve_file(doc, inline=True)


def _is_descendant(doc, root_folder):
    """True if doc lives in root_folder or any nested subfolder of it."""
    node = doc.folder
    while node is not None:
        if node.id == root_folder.id:
            return True
        node = node.parent
    return False


def _descendant_documents(root):
    """Every document under root (any depth), each with an archive name that is
    its path relative to root -- so a zip mirrors the shared folder's tree.

    Only walks the shared folder's own subtree, so it can never include a file
    outside the link's target (same guarantee as _is_descendant)."""
    results = []

    def walk(folder, prefix):
        for doc in folder.documents.all():
            results.append((doc, prefix + doc.name))
        for child in folder.children.all():
            walk(child, prefix + child.name + "/")

    walk(root, "")
    return results


def share_download_all(request, token):
    """Stream every file in a shared folder (and its subfolders) as one zip."""
    link = _valid_link(token)
    if not _is_unlocked(request, link):
        return redirect("share_view", token=token)
    if not link.folder_id:
        raise Http404

    docs = _descendant_documents(link.folder)
    if not docs:
        raise Http404("This folder has no files to download.")

    # Spill to disk past a small threshold so a large folder doesn't blow up
    # memory on a small instance; stored (no compression) keeps CPU low since
    # most uploads (images, PDFs) are already compressed.
    tmp = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
        for doc, arcname in docs:
            try:
                zf.write(doc.file.path, arcname)
            except (FileNotFoundError, ValueError):
                continue  # skip a missing/unreadable file rather than 500
    tmp.seek(0)

    filename = f"{link.folder.name or 'folder'}.zip"
    return FileResponse(
        tmp, as_attachment=True, filename=filename, content_type="application/zip"
    )
