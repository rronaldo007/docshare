import hashlib
import io
import mimetypes
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import get_valid_filename

from django.core.exceptions import PermissionDenied

import logging

from .forms import DocumentForm, EmailSettingsForm, FolderForm, ShareForm
from .models import Document, EmailSettings, Folder, ShareLink
from .permissions import is_admin

logger = logging.getLogger(__name__)


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


def _safe_content_type(upload=None, filename=None):
    """Derive a stored Content-Type WITHOUT trusting the client-supplied header.

    The browser-sent multipart Content-Type is attacker-controlled, so a file
    could be labeled text/html or image/svg+xml and later rendered inline as
    script. We instead guess from the (server-side) filename extension, and only
    accept a client header when it maps to a known-safe inline type. Everything
    else falls back to application/octet-stream and is served as a download.

    `upload` is optional: a chunked upload has no single upload object at
    finalize time, so it passes only the filename (extension-based guess only).
    """
    name = filename or (upload.name if upload else "") or ""
    guessed = mimetypes.guess_type(name)[0]
    if guessed and guessed.lower() in INLINE_CONTENT_TYPES:
        return guessed.lower()
    declared = (getattr(upload, "content_type", "") or "").split(";")[0].strip().lower()
    if declared in INLINE_CONTENT_TYPES:
        return declared
    return guessed or "application/octet-stream"


FILE_STREAM_CHUNK = 262144  # 256 KB


def _iter_file(name, storage):
    """Yield a stored object's bytes in chunks WITHOUT letting the backend buffer
    the whole (possibly multi-GB) object in memory.

    django-storages' S3 file downloads the ENTIRE object into a temp buffer on
    open() -- fine for a small photo, but a multi-GB file OOM-kills a small
    instance. So for an S3-compatible backend we read the boto3 streaming body
    directly (sequential, constant memory); local-disk storage already streams
    cheaply via read()."""
    bucket = getattr(storage, "bucket", None)
    if bucket is not None:
        body = bucket.Object(name).get()["Body"]
        try:
            yield from body.iter_chunks(chunk_size=FILE_STREAM_CHUNK)
        finally:
            body.close()
    else:
        f = storage.open(name, "rb")
        try:
            while True:
                chunk = f.read(FILE_STREAM_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            f.close()


def _serve_file(doc, *, inline):
    """Serve a document's bytes safely, streaming so even a multi-GB file uses
    constant memory.

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
    disposition = None
    if inline and kind == "image":
        content_type = (doc.content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    elif inline and kind == "pdf":
        content_type = "application/pdf"
    elif inline and kind == "text":
        content_type = "text/plain; charset=utf-8"
    else:
        content_type = "application/octet-stream"
        disposition = f'attachment; filename="{doc.name}"'

    response = StreamingHttpResponse(
        _iter_file(doc.file.name, doc.file.storage), content_type=content_type
    )
    if disposition:
        response["Content-Disposition"] = disposition
    if doc.size:  # let the browser show download progress / total
        response["Content-Length"] = str(doc.size)
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------- Authenticated browsing ----------

# Files per page in the browser. Keeps a folder of hundreds of photos to a
# manageable page (and bounds how many thumbnails load at once).
BROWSE_PAGE_SIZE = 25


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

    # Paginate the files so a folder of hundreds of photos loads a manageable
    # page at a time (and only ~one page of thumbnails is ever fetched at once).
    documents_qs = Document.objects.filter(owner=request.user, folder=current)
    page_obj = Paginator(documents_qs, BROWSE_PAGE_SIZE).get_page(request.GET.get("page"))
    documents = list(page_obj.object_list)
    # kind is a cheap property over content_type (no I/O); evaluate once so the
    # template can both list non-image files and render an image thumbnail grid.
    has_images = any(doc.kind == "image" for doc in documents)

    return render(
        request,
        "files/browse.html",
        {
            "current": current,
            "folders": folders,
            "documents": documents,
            "page_obj": page_obj,
            "has_images": has_images,
            "folder_form": FolderForm(),
            "document_form": DocumentForm(),
            "direct_upload": settings.DIRECT_UPLOAD_ENABLED,
            "direct_upload_max": settings.DIRECT_UPLOAD_MAX_BYTES,
            "direct_upload_part_size": settings.DIRECT_UPLOAD_PART_BYTES,
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


# ---------- Chunked upload (large files) ----------
#
# Cloudflare (in front of the app on Sevalla) rejects any single request body
# over ~100 MB, so a large file cannot be POSTed in one shot. The browser slices
# it into sub-100 MB chunks and sends them here one at a time; we append each to
# a single .part file on the persistent disk, then os.replace it straight into
# final storage (no second copy -- important for multi-GB files). The server
# keeps NO per-upload state between requests (the .part file's size IS the
# state), so it stays correct across multiple gunicorn workers.

CHUNK_STAGING_DIR = ".chunks"  # lives under MEDIA_ROOT, which is never web-served


def _chunk_part_path(user_id, upload_id):
    return os.path.join(
        settings.MEDIA_ROOT, CHUNK_STAGING_DIR, f"user_{user_id}", f"{upload_id}.part"
    )


def _validated_upload_id(raw):
    """Reject anything that is not a plain UUID, so a crafted upload_id can never
    traverse out of the per-user staging directory."""
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        return None


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _store_assembled_file(part_path, rel_dest):
    """Move a finished chunk-staging file into final storage; return its name.

    On local-disk storage this is an atomic rename within the same filesystem,
    so a multi-GB upload is never copied a second time. On object storage
    (S3/R2) there is no local destination path, so the assembled file is
    streamed up through the storage backend and the local staging file removed.
    The returned name is whatever the backend actually stored under (it may
    differ from rel_dest if the backend de-duplicates), so the caller records
    the real key on the Document.
    """
    try:
        abs_dest = default_storage.path(rel_dest)
    except NotImplementedError:
        abs_dest = None  # remote backend: no local filesystem path
    if abs_dest is not None:
        os.makedirs(os.path.dirname(abs_dest), exist_ok=True)
        os.replace(part_path, abs_dest)
        return rel_dest
    with open(part_path, "rb") as fh:
        stored = default_storage.save(rel_dest, File(fh))
    _safe_remove(part_path)
    return stored


# A Sevalla cron job runs as a separate process and CANNOT mount the persistent
# disk, so it could never see these staging files. Cleanup therefore runs inside
# the web process (which owns the disk): a throttled opportunistic sweep on the
# upload-complete path, plus the cleanup_chunks management command (run at deploy
# time / manually). Abandoned .part files only ever come from interrupted chunked
# uploads, so tying the sweep to the upload path covers the real case.
CHUNK_MAX_AGE_SECONDS = 24 * 3600
CHUNK_SWEEP_MIN_INTERVAL = 3600  # sweep at most once per hour
_CHUNK_SWEEP_MARKER = ".last_sweep"


def _sweep_stale_chunks(max_age_seconds):
    """Delete staging .part files last modified more than max_age_seconds ago.

    Returns the number removed. Shared by the cleanup_chunks command and the
    opportunistic sweep below. Idempotent and failure-tolerant, so it is safe to
    run from multiple workers at once."""
    root = os.path.join(settings.MEDIA_ROOT, CHUNK_STAGING_DIR)
    cutoff = time.time() - max_age_seconds
    removed = 0
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            if name == _CHUNK_SWEEP_MARKER:
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


def _maybe_sweep_stale_chunks():
    """Run a stale-chunk sweep at most once per CHUNK_SWEEP_MIN_INTERVAL, using a
    marker file's mtime as a cross-worker, cross-restart throttle. Cheap enough to
    call on every chunked-upload completion."""
    marker = os.path.join(settings.MEDIA_ROOT, CHUNK_STAGING_DIR, _CHUNK_SWEEP_MARKER)
    now = time.time()
    try:
        if os.path.exists(marker) and now - os.path.getmtime(marker) < CHUNK_SWEEP_MIN_INTERVAL:
            return
        # Touch the marker BEFORE sweeping so concurrent workers don't all sweep.
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "a"):
            os.utime(marker, None)
    except OSError:
        return
    _sweep_stale_chunks(CHUNK_MAX_AGE_SECONDS)


@login_required
def upload_chunk(request, folder_id=None):
    """Append one chunk of a large file to its staging .part file."""
    if request.method != "POST":
        return redirect("browse")
    upload_id = _validated_upload_id(request.POST.get("upload_id"))
    chunk = request.FILES.get("chunk")
    if upload_id is None or chunk is None:
        return HttpResponseBadRequest("Bad chunk upload.")

    part_path = _chunk_part_path(request.user.id, upload_id)
    os.makedirs(os.path.dirname(part_path), exist_ok=True)
    current = os.path.getsize(part_path) if os.path.exists(part_path) else 0

    # The client tells us the byte offset it believes it is writing at. If that
    # disagrees with what we already have, refuse rather than corrupt the file
    # by appending in the wrong place -- report our size so the client resyncs
    # and resumes (this is what makes a dropped chunk mid-upload recoverable).
    offset = request.POST.get("offset")
    if offset is not None:
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Bad offset.")
        if offset != current:
            return JsonResponse({"received": current}, status=409)

    # Optional hard cap so a single huge file can't silently fill the disk.
    max_bytes = getattr(settings, "MAX_UPLOAD_BYTES", 0)
    if max_bytes and current + chunk.size > max_bytes:
        _safe_remove(part_path)
        return HttpResponseBadRequest("File exceeds the maximum allowed size.")

    with open(part_path, "ab") as dest:
        for piece in chunk.chunks():
            dest.write(piece)

    return JsonResponse({"received": os.path.getsize(part_path)})


@login_required
def upload_chunk_complete(request, folder_id=None):
    """Finalize a chunked upload: move the assembled file into storage and create
    the Document. Mirrors upload_folder's path handling so a large file dropped
    inside a folder rebuilds its subfolder structure too."""
    if request.method != "POST":
        return redirect("browse")
    upload_id = _validated_upload_id(request.POST.get("upload_id"))
    if upload_id is None:
        return HttpResponseBadRequest("Bad upload id.")
    part_path = _chunk_part_path(request.user.id, upload_id)
    if not os.path.exists(part_path):
        raise Http404

    parent = None
    if folder_id is not None:
        parent = get_object_or_404(Folder, pk=folder_id, owner=request.user)

    # Sanitize the path exactly like upload_folder: drop empty/./.. segments so a
    # crafted name can never escape the user's own folder tree.
    rel = (request.POST.get("path") or "").replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    if not parts:
        _safe_remove(part_path)
        return HttpResponseBadRequest("Missing filename.")
    *dirs, filename = parts
    folder = _get_or_create_path(request.user, parent, dirs)

    size = os.path.getsize(part_path)
    # Build the same kind of unguessable, per-file storage path the model's
    # upload_to would, then place the already-assembled file there. On local
    # disk this is an atomic rename (no multi-GB second copy); on object storage
    # it streams the file up and drops the local staging copy. doc.name keeps
    # the original filename for the UI; the stored key uses a sanitized basename.
    safe_name = get_valid_filename(filename) or "file"
    rel_dest = f"user_{request.user.id}/{uuid.uuid4().hex}/{safe_name}"
    rel_dest = _store_assembled_file(part_path, rel_dest)

    doc = Document(
        owner=request.user,
        folder=folder,
        name=filename,
        size=size,
        content_type=_safe_content_type(filename=filename),
    )
    doc.file.name = rel_dest
    doc.save()

    # Opportunistically clear abandoned staging files (throttled, in-process).
    _maybe_sweep_stale_chunks()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "name": filename})
    messages.success(request, f"Uploaded '{filename}'.")
    return redirect(parent.get_absolute_url() if parent else "browse")


# ---------- Presigned direct-to-bucket upload (object storage only) ----------
#
# When DIRECT_UPLOAD_ENABLED, the browser uploads file bytes straight to the
# bucket and the app only mints the URL and records the result -- the bytes never
# transit this app or the ~100 MB proxy body limit. Both endpoints are gated on
# the flag, owner-scoped, and fail closed: the object key is server-minted under
# the user's own prefix, and commit refuses any key outside that prefix or any
# object that was not actually uploaded. Size is read from the bucket, never the
# client. (Buckets stay private; delivery still streams through _serve_file.)

def _s3_client():
    """The boto3 S3 client behind the default (object-storage) backend."""
    return default_storage.connection.meta.client


def _owned_object_key(user, key):
    """Validate a client-supplied object key is one we minted for this user.

    Returns the cleaned key, or None if it doesn't match the exact
    user_{id}/{hex}/{name} shape under the user's own prefix. Used to fail closed
    on any commit/multipart call: a client can never act on a key it didn't get
    from a server-minted presign for its own account.
    """
    key = (key or "").strip()
    prefix = f"user_{user.id}/"
    if not key.startswith(prefix) or ".." in key or key.count("/") != 2:
        return None
    return key


def _presigned_put_url(key):
    """Short-lived presigned S3/R2 PUT URL for one server-chosen object key."""
    return _s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": default_storage.bucket_name, "Key": key},
        ExpiresIn=settings.DIRECT_UPLOAD_EXPIRY,
    )


@login_required
def presign_upload(request, folder_id=None):
    """Return a presigned PUT URL plus the object key the browser must upload to.

    The key is generated server-side under user_{id}/ so a client can never
    presign a write outside its own prefix; the filename only contributes a
    sanitized basename for display/extension.
    """
    if not settings.DIRECT_UPLOAD_ENABLED:
        raise Http404
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")
    if folder_id is not None:
        get_object_or_404(Folder, pk=folder_id, owner=request.user)

    raw = (request.POST.get("filename") or "").replace("\\", "/")
    safe_name = get_valid_filename(os.path.basename(raw)) or "file"
    key = f"user_{request.user.id}/{uuid.uuid4().hex}/{safe_name}"
    return JsonResponse({"url": _presigned_put_url(key), "key": key})


@login_required
def commit_upload(request, folder_id=None):
    """Record a Document for a file the browser uploaded directly to the bucket.

    Fails closed: the key MUST sit inside this user's own prefix and match the
    exact server-minted shape, and the object MUST actually exist in the bucket.
    The stored size is read from the bucket, never trusted from the client. The
    path rebuilds the subfolder tree exactly like upload_folder / chunk-complete.
    """
    if not settings.DIRECT_UPLOAD_ENABLED:
        raise Http404
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")
    parent = None
    if folder_id is not None:
        parent = get_object_or_404(Folder, pk=folder_id, owner=request.user)

    # The key was minted by presign_upload as user_{id}/{hex}/{name}: exactly two
    # slashes, this user's prefix, no traversal. Reject anything else so a client
    # cannot claim another user's object or an arbitrary key.
    key = _owned_object_key(request.user, request.POST.get("key"))
    if key is None:
        return HttpResponseBadRequest("Bad object key.")
    if not default_storage.exists(key):
        raise Http404  # nothing was actually uploaded under this key

    rel = (request.POST.get("path") or "").replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    if not parts:
        return HttpResponseBadRequest("Missing filename.")
    *dirs, filename = parts
    folder = _get_or_create_path(request.user, parent, dirs)

    # MAX_UPLOAD_BYTES guards the chunked uploader's local-disk staging only.
    # Direct uploads stream straight to the bucket and never touch the disk, so
    # that cap does not apply here -- size is read from the bucket for the record.
    size = default_storage.size(key)  # authoritative, from the bucket

    doc = Document(
        owner=request.user,
        folder=folder,
        name=filename,
        size=size,
        content_type=_safe_content_type(filename=filename),
    )
    doc.file.name = key
    doc.save()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "name": filename})
    messages.success(request, f"Uploaded '{filename}'.")
    return redirect(parent.get_absolute_url() if parent else "browse")


# ---------- Presigned multipart direct upload (object storage, > 5 GB) ----------
#
# A single PUT caps at 5 GB; for larger files the browser uploads the object to
# the bucket in parts via S3/R2 multipart. The app only brokers the session: it
# mints the key (server-side, under the user's prefix), hands out one short-lived
# presigned PUT per part, and finalizes by reading the authoritative part list
# AND size from the bucket -- never from the client. No file bytes transit the
# app or the local disk, so file size is decoupled from the box's RAM/disk. The
# server keeps NO state between calls: the client carries (key, upload_id) and we
# re-validate the key on every endpoint, so it stays correct across workers. All
# four endpoints are @login_required and 404 when the flag is off.

# S3/R2 allow at most 10,000 parts per multipart upload.
MULTIPART_MAX_PARTS = 10000


@login_required
def multipart_create(request, folder_id=None):
    """Open a multipart upload: mint the object key and return its R2 UploadId."""
    if not settings.DIRECT_UPLOAD_ENABLED:
        raise Http404
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")
    if folder_id is not None:
        get_object_or_404(Folder, pk=folder_id, owner=request.user)

    raw = (request.POST.get("filename") or "").replace("\\", "/")
    safe_name = get_valid_filename(os.path.basename(raw)) or "file"
    key = f"user_{request.user.id}/{uuid.uuid4().hex}/{safe_name}"
    resp = _s3_client().create_multipart_upload(
        Bucket=default_storage.bucket_name, Key=key
    )
    return JsonResponse(
        {
            "key": key,
            "upload_id": resp["UploadId"],
            "part_size": settings.DIRECT_UPLOAD_PART_BYTES,
        }
    )


@login_required
def multipart_sign_part(request, folder_id=None):
    """Return a presigned PUT URL for one part of an in-progress upload.

    Signed per part (not all upfront) so a long upload survives URL expiry: the
    client requests a fresh URL right before sending each part. The key is
    re-validated against the caller's prefix so a client cannot sign a write to
    another user's object.
    """
    if not settings.DIRECT_UPLOAD_ENABLED:
        raise Http404
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")
    key = _owned_object_key(request.user, request.POST.get("key"))
    upload_id = (request.POST.get("upload_id") or "").strip()
    if key is None or not upload_id:
        return HttpResponseBadRequest("Bad multipart request.")
    try:
        part_number = int(request.POST.get("part_number"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Bad part number.")
    if not 1 <= part_number <= MULTIPART_MAX_PARTS:
        return HttpResponseBadRequest("Bad part number.")

    url = _s3_client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": default_storage.bucket_name,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=settings.DIRECT_UPLOAD_EXPIRY,
    )
    return JsonResponse({"url": url})


@login_required
def multipart_complete(request, folder_id=None):
    """Finalize a multipart upload and record the Document.

    Fails closed: the key MUST be one we minted for this user, and the part list
    and size are read from the bucket (never trusted from the client). Rebuilds
    the subfolder tree from path exactly like commit_upload / chunk-complete.
    """
    if not settings.DIRECT_UPLOAD_ENABLED:
        raise Http404
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")
    parent = None
    if folder_id is not None:
        parent = get_object_or_404(Folder, pk=folder_id, owner=request.user)

    key = _owned_object_key(request.user, request.POST.get("key"))
    upload_id = (request.POST.get("upload_id") or "").strip()
    if key is None or not upload_id:
        return HttpResponseBadRequest("Bad multipart request.")

    rel = (request.POST.get("path") or "").replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    if not parts:
        return HttpResponseBadRequest("Missing filename.")
    *dirs, filename = parts

    client = _s3_client()
    bucket = default_storage.bucket_name

    # The object-store calls can fail for reasons outside our control (R2 quirks,
    # transient errors). Surface a concise reason to the client and log the full
    # traceback rather than returning an opaque 500, so finalize is diagnosable.
    try:
        # Read the parts R2 actually received -- we never trust a client part
        # list. list_parts pages at 1000 entries, so follow the truncation marker.
        part_items = []
        marker = None
        while True:
            kwargs = {"Bucket": bucket, "Key": key, "UploadId": upload_id}
            if marker is not None:
                kwargs["PartNumberMarker"] = marker
            listed = client.list_parts(**kwargs)
            part_items.extend(listed.get("Parts", []))
            if not listed.get("IsTruncated"):
                break
            marker = listed["NextPartNumberMarker"]

        if not part_items:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
            return HttpResponseBadRequest("No parts uploaded.")

        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": p["PartNumber"], "ETag": p["ETag"]}
                    for p in part_items
                ]
            },
        )
    except Exception as exc:  # noqa: BLE001 -- report the real reason, don't 500 opaquely
        logger.exception("Multipart finalize failed for key %s", key)
        # Return 200 with an error flag rather than a 5xx: an edge proxy
        # (Cloudflare) may replace a 5xx body with its own page, which would hide
        # the real reason from the client. A 200 body always reaches the browser.
        return JsonResponse(
            {"ok": False, "error": f"Could not finalize the upload: {exc}"}
        )

    folder = _get_or_create_path(request.user, parent, dirs)
    # MAX_UPLOAD_BYTES guards the chunked uploader's local-disk staging only;
    # multipart bytes go straight to the bucket and never touch the disk, so the
    # cap does not apply here (it would defeat the whole point of multipart).
    size = default_storage.size(key)  # authoritative, from the bucket

    doc = Document(
        owner=request.user,
        folder=folder,
        name=filename,
        size=size,
        content_type=_safe_content_type(filename=filename),
    )
    doc.file.name = key
    doc.save()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "name": filename})
    messages.success(request, f"Uploaded '{filename}'.")
    return redirect(parent.get_absolute_url() if parent else "browse")


@login_required
def multipart_abort(request, folder_id=None):
    """Abort an in-progress multipart upload so R2 keeps no orphaned parts."""
    if not settings.DIRECT_UPLOAD_ENABLED:
        raise Http404
    if request.method != "POST":
        return HttpResponseBadRequest("POST required.")
    key = _owned_object_key(request.user, request.POST.get("key"))
    upload_id = (request.POST.get("upload_id") or "").strip()
    if key is None or not upload_id:
        return HttpResponseBadRequest("Bad multipart request.")
    _s3_client().abort_multipart_upload(
        Bucket=default_storage.bucket_name, Key=key, UploadId=upload_id
    )
    return JsonResponse({"ok": True})


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


ZIP_CONTENT_TYPES = {
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "multipart/x-zip",
}
ZIP_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# How many images to show in the zip gallery preview, and the most entries we'll
# list (a pathological archive could have a huge directory).
ZIP_GALLERY_COUNT = 12
ZIP_ENTRY_LIMIT = 2000


def _is_zip(doc):
    name = (doc.name or doc.file.name or "").lower()
    ct = (doc.content_type or "").split(";")[0].strip().lower()
    return name.endswith(".zip") or ct in ZIP_CONTENT_TYPES


def _is_zip_image(name):
    return os.path.splitext(name)[1].lower() in ZIP_IMAGE_EXTS


class _S3RangeReader(io.RawIOBase):
    """A seekable, read-only file over an S3/R2 object that fetches byte ranges
    on demand. Lets zipfile read a huge archive's directory and pull individual
    entries via HTTP range requests, instead of downloading the whole object."""

    def __init__(self, obj, size):
        self._obj = obj
        self._size = size
        self._pos = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        return self._pos

    def tell(self):
        return self._pos

    def read(self, size=-1):
        if self._pos >= self._size:
            return b""
        end = self._size - 1 if size is None or size < 0 else min(self._pos + size, self._size) - 1
        if end < self._pos:
            return b""
        body = self._obj.get(Range=f"bytes={self._pos}-{end}")["Body"]
        try:
            data = body.read()
        finally:
            body.close()
        self._pos += len(data)
        return data


def _open_zip(doc):
    """Open a zip document as a ZipFile WITHOUT buffering the whole archive: for
    S3 storage, read it through range requests; local disk is already seekable.
    Returns (zipfile, underlying_fp); the caller closes both."""
    storage = doc.file.storage
    name = doc.file.name
    bucket = getattr(storage, "bucket", None)
    if bucket is not None:
        size = doc.size or storage.size(name)
        fp = _S3RangeReader(bucket.Object(name), size)
    else:
        fp = storage.open(name, "rb")
    return zipfile.ZipFile(fp), fp


def _zip_listing(doc):
    """Return (entries, gallery_images, truncated) for a zip: a capped file list
    plus the first few image entries to show as a gallery. Reads only the archive
    directory, so it stays cheap even for a multi-GB zip."""
    zf, fp = _open_zip(doc)
    try:
        entries, images, truncated = [], [], False
        for info in zf.infolist():
            if info.is_dir():
                continue
            if len(entries) >= ZIP_ENTRY_LIMIT:
                truncated = True
                break
            item = {
                "name": info.filename,
                "size": info.file_size,
                "is_image": _is_zip_image(info.filename),
            }
            entries.append(item)
            if item["is_image"] and len(images) < ZIP_GALLERY_COUNT:
                images.append(item)
        return entries, images, truncated
    finally:
        zf.close()
        fp.close()


@login_required
def preview_document(request, doc_id):
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    ctx = {"doc": doc}
    if _is_zip(doc):
        ctx["is_zip"] = True
        try:
            entries, images, truncated = _zip_listing(doc)
            ctx["zip_entries"] = entries
            ctx["zip_gallery"] = images
            ctx["zip_truncated"] = truncated
        except Exception:
            ctx["zip_error"] = True
    return render(request, "files/preview.html", ctx)


def _zip_entry_response(doc, entry):
    """Build a streaming response for one entry inside a zip doc, or raise Http404.
    zipfile only reads entries that actually exist in the archive (a path not in
    the zip raises -> no client path is trusted), and the same inline allowlist as
    other files applies -- images/PDF/plain text inline, everything else (incl.
    HTML/SVG) forced to download -- so an entry can never execute as same-origin
    script. Reads only the requested entry from R2 (range requests), never the
    whole archive. Callers do their own auth (owner-scope or share-link guards)."""
    if not _is_zip(doc) or not entry or entry.endswith("/"):
        raise Http404
    try:
        zf, fp = _open_zip(doc)
    except Exception:
        raise Http404("Not a readable zip.")
    if entry not in zf.namelist():
        zf.close()
        fp.close()
        raise Http404

    filename = os.path.basename(entry) or "file"
    guessed = (mimetypes.guess_type(filename)[0] or "").lower()
    inline = guessed in INLINE_CONTENT_TYPES
    content_type = guessed if inline else "application/octet-stream"

    def stream():
        try:
            with zf.open(entry) as src:
                while True:
                    chunk = src.read(262144)
                    if not chunk:
                        break
                    yield chunk
        finally:
            zf.close()
            fp.close()

    response = StreamingHttpResponse(stream(), content_type=content_type)
    if not inline:
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def zip_entry(request, doc_id):
    """Owner: stream one file from inside one of the user's own zip documents."""
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    return _zip_entry_response(doc, request.GET.get("path", ""))


def _zip_thumb_key(doc, entry):
    h = hashlib.md5(entry.encode("utf-8", "replace")).hexdigest()
    return f"thumbnails/zip/{doc.id}/{h}.jpg"


def _generate_zip_thumbnail(doc, entry, key):
    """Make a small JPEG thumbnail for one image inside a zip and cache it.
    Reads only that entry (range requests), copies it to a temp file (low
    memory), then thumbnails with JPEG draft mode -- same memory discipline as
    document thumbnails."""
    from io import BytesIO

    from PIL import Image, ImageOps

    zf, fp = _open_zip(doc)
    try:
        with zf.open(entry) as src, tempfile.NamedTemporaryFile(suffix=".img") as tmp:
            shutil.copyfileobj(src, tmp, length=1024 * 1024)
            tmp.flush()
            tmp.seek(0)
            img = Image.open(tmp)
            img.draft("RGB", (THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=80, optimize=True)
    finally:
        zf.close()
        fp.close()
    buf.seek(0)
    default_storage.save(key, File(buf))


def _zip_thumbnail_response(doc, entry):
    """Serve a cached JPEG thumbnail for one image inside a zip, or raise Http404.
    Only image entries; zipfile only reads entries that exist. Callers do auth."""
    if not _is_zip(doc) or not entry or not _is_zip_image(entry):
        raise Http404
    key = _zip_thumb_key(doc, entry)
    try:
        if not default_storage.exists(key):
            _generate_zip_thumbnail(doc, entry, key)
        response = FileResponse(default_storage.open(key, "rb"), content_type="image/jpeg")
    except Exception:
        raise Http404("Could not render preview.")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=86400"
    return response


@login_required
def zip_thumbnail(request, doc_id):
    """Owner: cached thumbnail for one image inside one of the user's zips."""
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    return _zip_thumbnail_response(doc, request.GET.get("path", ""))


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


# Longest-edge size (px) for the grid thumbnails. Small enough that a folder of
# hundreds of photos loads quickly, large enough to stay crisp on a 2x display.
THUMBNAIL_MAX_PX = 400


def _thumb_key(doc):
    """Storage key for a document's cached thumbnail. Derived server-side from
    the stored object key (never client-supplied), kept under a thumbnails/
    prefix so it never collides with originals and stays private in the bucket."""
    return f"thumbnails/{doc.file.name}.jpg"


def _generate_thumbnail(doc, key):
    """Make a small JPEG thumbnail from the original image and cache it in the
    default storage.

    Memory matters here: concurrent generations from a folder of many photos
    were OOM-killing the worker on a small instance. So we copy the original to
    a local temp file in chunks (never holding the whole multi-MB image in RAM),
    then let Pillow read from disk with JPEG draft mode, which decodes at a
    reduced scale. Peak memory per generation stays tiny."""
    import shutil
    import tempfile
    from io import BytesIO

    from PIL import Image, ImageOps

    with tempfile.NamedTemporaryFile(suffix=".src") as tmp:
        with doc.file.open("rb") as src:
            shutil.copyfileobj(src, tmp, length=1024 * 1024)
        tmp.flush()
        tmp.seek(0)
        img = Image.open(tmp)
        img.draft("RGB", (THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
        img = ImageOps.exif_transpose(img)  # honor camera rotation
        img = img.convert("RGB")
        img.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=True)
    buf.seek(0)
    default_storage.save(key, File(buf))


@login_required
def thumbnail_document(request, doc_id):
    """Serve a small cached JPEG thumbnail for an image document (used by the
    browse grid). Generated on first view and cached in the private bucket;
    owner-scoped. Non-images and any thumbnailing failure fall back to the
    normal guarded inline serve, so the grid degrades gracefully."""
    doc = get_object_or_404(Document, pk=doc_id, owner=request.user)
    if doc.kind != "image":
        return _serve_file(doc, inline=True)
    key = _thumb_key(doc)
    try:
        if not default_storage.exists(key):
            _generate_thumbnail(doc, key)
        response = FileResponse(
            default_storage.open(key, "rb"), content_type="image/jpeg"
        )
    except Exception:
        return _serve_file(doc, inline=True)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=86400"
    return response


# ---------- Sharing ----------

def _email_connection_and_from():
    """Build an SMTP connection + From address from the UI-configured EmailSettings
    when it's enabled; otherwise return (None, default) so the app's configured
    backend (console by default, or DJANGO_EMAIL_* env SMTP) is used."""
    from django.core.mail import get_connection

    cfg = EmailSettings.load()
    if cfg.enabled and cfg.host:
        conn = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            password=cfg.password,
            use_tls=cfg.use_tls,
        )
        from_email = cfg.from_email or cfg.username or settings.DEFAULT_FROM_EMAIL
        return conn, from_email
    return None, settings.DEFAULT_FROM_EMAIL


def _send_email(subject, body, recipient):
    """Send one plain-text email through the UI-configured SMTP (or the app
    backend if not configured). Raises on failure so callers can report it."""
    from django.core.mail import EmailMessage

    conn, from_email = _email_connection_and_from()
    EmailMessage(subject, body, from_email, [recipient], connection=conn).send(
        fail_silently=False
    )


def _email_share_link(request, recipient, target, link, share_url):
    """Email a freshly created share link to the recipient. Never let a mail
    failure break link creation -- the link already exists and its URL is shown;
    we just report whether the email went out."""
    sender_name = request.user.get_username()
    label = target.name
    subject = f'{sender_name} shared "{label}" with you on DocShare'
    lines = [
        f"{sender_name} shared {label} with you on DocShare.",
        "",
        f"Open it here: {share_url}",
    ]
    if link.requires_password:
        lines.append("\nThis link is password protected; ask the sender for the password.")
    if link.expires_at:
        lines.append(f"\nThe link expires on {link.expires_at:%b %d, %Y}.")
    try:
        _send_email(subject, "\n".join(lines), recipient)
        messages.success(request, f"Link emailed to {recipient}.")
    except Exception:
        messages.warning(
            request, f"Link created, but emailing {recipient} failed."
        )


@login_required
def email_settings(request):
    """Configure outgoing email (SMTP) from the UI. Admin-only -- this is an
    app-wide setting and the app allows public signup, so a regular user must
    not be able to read/change the mail server or send as the app."""
    if not is_admin(request.user):
        raise PermissionDenied
    cfg = EmailSettings.load()
    if request.method == "POST":
        old_password = cfg.password
        form = EmailSettingsForm(request.POST, instance=cfg)
        if form.is_valid():
            obj = form.save(commit=False)
            if not form.cleaned_data.get("password"):
                obj.password = old_password  # blank = keep existing
            obj.save()
            messages.success(request, "Email settings saved.")
            return redirect("email_settings")
    else:
        form = EmailSettingsForm(instance=cfg)
    return render(request, "files/email_settings.html", {"form": form, "cfg": cfg})


@login_required
def send_test_email(request):
    if not is_admin(request.user):
        raise PermissionDenied
    if request.method != "POST":
        return redirect("email_settings")
    recipient = (request.POST.get("to") or request.user.email or "").strip()
    if not recipient:
        messages.error(request, "Enter a recipient address for the test.")
        return redirect("email_settings")
    try:
        _send_email(
            "DocShare test email",
            "This is a test email from your DocShare email settings. "
            "If you received it, sending works.",
            recipient,
        )
        messages.success(request, f"Test email sent to {recipient}.")
    except Exception as exc:  # noqa: BLE001 - surface the SMTP error to the user
        messages.error(request, f"Test failed: {exc}")
    return redirect("email_settings")


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

    recipient = form.cleaned_data.get("email")
    if recipient:
        _email_share_link(request, recipient, target, link, share_url)

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


@login_required
def remove_link_password(request, token):
    """Clear the password on one of the owner's links, making it public, without
    revoking and recreating it. Owner-scoped and POST-only like revoke_link."""
    if request.method != "POST":
        return redirect("my_links")
    link = get_object_or_404(ShareLink, token=token, created_by=request.user)
    if link.requires_password:
        link.set_password("")  # empty leaves the link public
        link.save(update_fields=["password"])
        messages.success(request, "Password removed; the link is now public.")
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
        doc = link.document
        ctx = {"link": link, "doc": doc}
        if _is_zip(doc):
            ctx["is_zip"] = True
            try:
                entries, images, truncated = _zip_listing(doc)
                ctx["zip_entries"] = entries
                ctx["zip_gallery"] = images
                ctx["zip_truncated"] = truncated
            except Exception:
                ctx["zip_error"] = True
        return render(request, "files/share_document.html", ctx)

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


def share_zip_entry(request, token, doc_id):
    """Public: stream one entry from inside a shared zip. Same share-link guards
    as share_download (valid + unlocked + the doc must be the link's target), so
    it never exposes anything beyond the shared zip itself."""
    link = _valid_link(token)
    if not _is_unlocked(request, link):
        return redirect("share_view", token=token)
    doc = _shared_doc_or_404(link, doc_id)
    return _zip_entry_response(doc, request.GET.get("path", ""))


def share_zip_thumbnail(request, token, doc_id):
    """Public: cached thumbnail for one image inside a shared zip (gallery)."""
    link = _valid_link(token)
    if not _is_unlocked(request, link):
        return redirect("share_view", token=token)
    doc = _shared_doc_or_404(link, doc_id)
    return _zip_thumbnail_response(doc, request.GET.get("path", ""))


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


class _UnseekableZipSink:
    """A write-only sink for ZipFile. It provides tell() for offset bookkeeping
    but no seek(), so ZipFile streams with data descriptors and never rewinds --
    letting us build the zip on the fly and yield it chunk by chunk."""

    def __init__(self):
        self._chunks = []
        self._pos = 0

    def write(self, data):
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def flush(self):
        pass

    def seekable(self):
        return False

    def drain(self):
        chunks, self._chunks = self._chunks, []
        return b"".join(chunks)


def _zip_stream(docs):
    """Yield a zip of `docs` incrementally (constant memory, no temp file), so
    a large 'Download all' starts sending immediately -- avoiding proxy/worker
    timeouts and disk pressure that buffering the whole archive would cause."""
    sink = _UnseekableZipSink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    for doc, arcname in docs:
        try:
            src = doc.file.open("rb")
        except (FileNotFoundError, OSError, ValueError):
            continue  # skip a missing/unreadable file rather than 500
        try:
            with zf.open(arcname, "w") as dest:
                while True:
                    chunk = src.read(262144)
                    if not chunk:
                        break
                    dest.write(chunk)
                    data = sink.drain()
                    if data:
                        yield data
        finally:
            src.close()
        data = sink.drain()
        if data:
            yield data
    zf.close()
    data = sink.drain()
    if data:
        yield data


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

    name = (link.folder.name or "folder").replace('"', "").replace("\\", "")
    response = StreamingHttpResponse(
        _zip_stream(docs), content_type="application/zip"
    )
    response["Content-Disposition"] = f'attachment; filename="{name}.zip"'
    return response


@login_required
def download_folder_zip(request, folder_id):
    """Stream one of the owner's own folders (and its subfolders) as a single
    zip, built on the fly in constant memory -- so you can grab a whole folder
    of files without pre-zipping and re-uploading. Owner-scoped, same streaming
    engine as the public 'Download all'."""
    folder = get_object_or_404(Folder, pk=folder_id, owner=request.user)
    docs = _descendant_documents(folder)
    if not docs:
        messages.error(request, "This folder has no files to download.")
        return redirect(folder.get_absolute_url())

    name = (folder.name or "folder").replace('"', "").replace("\\", "")
    response = StreamingHttpResponse(
        _zip_stream(docs), content_type="application/zip"
    )
    response["Content-Disposition"] = f'attachment; filename="{name}.zip"'
    return response
