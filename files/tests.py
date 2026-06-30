import os
import shutil
import tempfile
import uuid

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Document, Folder, ShareLink


class RemoteLikeStorage(Storage):
    """Minimal dict-backed storage that mimics S3/R2: like the real backend it
    exposes no local filesystem path(), so the chunked uploader must stream the
    assembled file up rather than os.replace it. Bytes are shared at class level
    so the lazy default-storage proxy and the FileField resolve to the same data."""

    _files = {}

    def _open(self, name, mode="rb"):
        return ContentFile(self._files[name])

    def _save(self, name, content):
        content.seek(0)
        self._files[name] = content.read()
        return name

    def exists(self, name):
        return name in self._files

    def delete(self, name):
        self._files.pop(name, None)

    def size(self, name):
        return len(self._files[name])

    def path(self, name):
        raise NotImplementedError("This backend doesn't support absolute paths.")


class ShareLinkIsolationTests(TestCase):
    """A folder share link must never expose a file outside its target."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.shared_folder = Folder.objects.create(name="Shared", owner=self.owner)
        self.other_folder = Folder.objects.create(name="Private", owner=self.owner)
        self.secret_doc = Document.objects.create(
            name="secret.txt",
            file=SimpleUploadedFile("secret.txt", b"top secret"),
            folder=self.other_folder,
            owner=self.owner,
        )
        self.folder_link = ShareLink.objects.create(
            folder=self.shared_folder, created_by=self.owner
        )

    def test_file_outside_shared_folder_is_blocked(self):
        url = reverse("share_download", args=[self.folder_link.token, self.secret_doc.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


class PasswordProtectedLinkTests(TestCase):
    """A protected link gates its target behind the owner-chosen password."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.shared_folder = Folder.objects.create(name="Shared", owner=self.owner)
        self.shared_doc = Document.objects.create(
            name="shared.txt",
            file=SimpleUploadedFile("shared.txt", b"hello world"),
            folder=self.shared_folder,
            owner=self.owner,
        )
        self.link = ShareLink(folder=self.shared_folder, created_by=self.owner)
        self.link.set_password("letmein")
        self.link.save()

    def test_correct_password_grants_access(self):
        view_url = reverse("share_view", args=[self.link.token])
        download_url = reverse("share_download", args=[self.link.token, self.shared_doc.id])

        # Locked until the password is supplied.
        self.assertEqual(self.client.get(download_url).status_code, 302)

        resp = self.client.post(view_url, {"password": "letmein"})
        self.assertEqual(resp.status_code, 302)

        # Now the shared file is reachable.
        self.assertEqual(self.client.get(download_url).status_code, 200)

    def test_wrong_password_is_blocked(self):
        view_url = reverse("share_view", args=[self.link.token])
        download_url = reverse("share_download", args=[self.link.token, self.shared_doc.id])

        resp = self.client.post(view_url, {"password": "wrong"})
        # Re-renders the prompt rather than unlocking.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Incorrect password")

        # File stays gated: download redirects back to the prompt.
        blocked = self.client.get(download_url)
        self.assertEqual(blocked.status_code, 302)
        self.assertEqual(blocked["Location"], view_url)


class AnonymousUploadTests(TestCase):
    """Anyone can upload a file and get a public link without an account."""

    def _upload(self, name="anon.txt", body=b"hello"):
        return self.client.post(
            reverse("anonymous_upload"),
            {"file": SimpleUploadedFile(name, body)},
        )

    def test_anonymous_upload_creates_working_link(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, 302)

        link = ShareLink.objects.get()
        self.assertEqual(link.created_by.username, "anonymous")
        self.assertEqual(link.document.owner.username, "anonymous")
        self.assertIsNotNone(link.expires_at)  # anonymous links self-expire

        # The minted public link serves the file with no login.
        download_url = reverse("share_download", args=[link.token, link.document.id])
        self.assertEqual(self.client.get(download_url).status_code, 200)

    def test_daily_limit_blocks_extra_uploads(self):
        for _ in range(5):
            self.assertEqual(self._upload().status_code, 302)
        self.assertEqual(Document.objects.count(), 5)

        # The sixth upload from the same IP today is rejected; nothing is stored.
        self._upload()
        self.assertEqual(Document.objects.count(), 5)


_CHUNK_MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=_CHUNK_MEDIA)
class ChunkedUploadTests(TestCase):
    """Large files upload as a sequence of chunks the server reassembles."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_CHUNK_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)
        self.upload_id = str(uuid.uuid4())

    def _send_chunk(self, offset, body):
        return self.client.post(
            reverse("upload_chunk_root"),
            {
                "upload_id": self.upload_id,
                "offset": offset,
                "chunk": SimpleUploadedFile("chunk", body),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def _complete(self, path="big.txt"):
        return self.client.post(
            reverse("upload_chunk_complete_root"),
            {"upload_id": self.upload_id, "path": path},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_chunks_reassemble_into_one_document(self):
        self.assertEqual(self._send_chunk(0, b"hello ").status_code, 200)
        self.assertEqual(self._send_chunk(6, b"world").status_code, 200)
        self.assertEqual(self._complete("greeting.txt").status_code, 200)

        doc = Document.objects.get()
        self.assertEqual(doc.owner, self.user)
        self.assertEqual(doc.name, "greeting.txt")
        self.assertEqual(doc.size, 11)
        self.assertEqual(doc.file.open("rb").read(), b"hello world")

    def test_offset_mismatch_is_rejected_for_resync(self):
        self.assertEqual(self._send_chunk(0, b"hello ").status_code, 200)
        # A chunk claiming the wrong offset must not corrupt the file; the server
        # replies 409 with the byte count it actually has so the client resyncs.
        resp = self._send_chunk(999, b"world")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["received"], 6)

    def test_complete_without_staged_file_is_404(self):
        self.assertEqual(self._complete().status_code, 404)

    def test_bad_upload_id_is_rejected(self):
        resp = self.client.post(
            reverse("upload_chunk_root"),
            {"upload_id": "../../etc/passwd", "offset": 0,
             "chunk": SimpleUploadedFile("chunk", b"x")},
        )
        self.assertEqual(resp.status_code, 400)

    def test_path_traversal_in_filename_is_neutralized(self):
        self.assertEqual(self._send_chunk(0, b"data").status_code, 200)
        self.assertEqual(self._complete("../../../etc/passwd").status_code, 200)

        # The '..' segments are stripped: the file lands inside the user's tree
        # as a folder "etc" containing "passwd", never outside MEDIA_ROOT.
        doc = Document.objects.get()
        self.assertEqual(doc.name, "passwd")
        self.assertTrue(doc.file.name.startswith(f"user_{self.user.id}/"))
        self.assertEqual(doc.folder.name, "etc")

    def test_chunk_upload_requires_login(self):
        self.client.logout()
        resp = self._send_chunk(0, b"x")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_large_file_in_folder_rebuilds_subfolder_tree(self):
        # A big file uploaded as part of a folder carries its relative path
        # ("Photos/2024/clip.mp4"); the chunked-complete view must rebuild the
        # nested folders and place the document in the deepest one, exactly like
        # a small-file folder upload does.
        self.assertEqual(self._send_chunk(0, b"movie ").status_code, 200)
        self.assertEqual(self._send_chunk(6, b"bytes").status_code, 200)
        self.assertEqual(self._complete("Photos/2024/clip.mp4").status_code, 200)

        photos = Folder.objects.get(name="Photos", parent=None, owner=self.user)
        year = Folder.objects.get(name="2024", parent=photos, owner=self.user)
        doc = Document.objects.get()
        self.assertEqual(doc.name, "clip.mp4")
        self.assertEqual(doc.folder, year)
        self.assertEqual(doc.file.open("rb").read(), b"movie bytes")

    def test_large_file_uploads_into_existing_folder(self):
        # Uploading a big file while browsing inside a folder uses the
        # folder-scoped chunk URLs; the assembled document must nest under that
        # parent and reuse existing subfolders rather than duplicate them.
        parent = Folder.objects.create(name="Trip", owner=self.user)
        existing = Folder.objects.create(name="Day1", parent=parent, owner=self.user)

        self.assertEqual(
            self.client.post(
                reverse("upload_chunk", args=[parent.id]),
                {
                    "upload_id": self.upload_id,
                    "offset": 0,
                    "chunk": SimpleUploadedFile("chunk", b"raw"),
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                reverse("upload_chunk_complete", args=[parent.id]),
                {"upload_id": self.upload_id, "path": "Day1/photo.raw"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            ).status_code,
            200,
        )

        doc = Document.objects.get()
        self.assertEqual(doc.folder, existing)  # reused, not duplicated
        self.assertEqual(Folder.objects.filter(name="Day1").count(), 1)


# The real S3/R2 backend has no usable local path(): it raises NotImplementedError,
# which is how the chunked uploader detects "remote" storage and streams the file
# up instead of os.replace-ing it. Django's plain InMemoryStorage DOES return a
# path, so it would take the local branch and not match production; subclassing to
# raise NotImplementedError makes it a faithful, network-free stand-in for S3.
@override_settings(
    MEDIA_ROOT=_CHUNK_MEDIA,
    STORAGES={
        "default": {"BACKEND": "files.tests.RemoteLikeStorage"},
        "staticfiles": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    },
)
class ChunkedUploadToObjectStorageTests(TestCase):
    """Large files finalize correctly when files live in object storage."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_CHUNK_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        RemoteLikeStorage._files = {}
        User = get_user_model()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)
        self.upload_id = str(uuid.uuid4())

    def _send_chunk(self, offset, body):
        return self.client.post(
            reverse("upload_chunk_root"),
            {
                "upload_id": self.upload_id,
                "offset": offset,
                "chunk": SimpleUploadedFile("chunk", body),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_assembled_file_is_streamed_into_object_storage(self):
        # Chunks stage on the local disk, then the finished file is uploaded to
        # the (non-path) backend on complete and read back through it.
        self.assertEqual(self._send_chunk(0, b"remote ").status_code, 200)
        self.assertEqual(self._send_chunk(7, b"bytes").status_code, 200)
        resp = self.client.post(
            reverse("upload_chunk_complete_root"),
            {"upload_id": self.upload_id, "path": "Albums/big.bin"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)

        doc = Document.objects.get()
        self.assertEqual(doc.size, 12)
        self.assertEqual(doc.folder.name, "Albums")
        self.assertEqual(doc.file.open("rb").read(), b"remote bytes")

        # The local staging .part must be gone (no orphaned second copy).
        chunks_dir = os.path.join(_CHUNK_MEDIA, ".chunks")
        leftover = []
        for root, _dirs, files in os.walk(chunks_dir):
            leftover += [f for f in files if f.endswith(".part")]
        self.assertEqual(leftover, [])


# A real S3Storage configured with dummy creds: presigning is a local HMAC, so
# it generates a valid URL without any network. Used to exercise presign_upload.
_S3_TEST_STORAGE = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": "test-bucket",
            "access_key": "k",
            "secret_key": "s",
            "endpoint_url": "https://acc.r2.cloudflarestorage.com",
            "region_name": "auto",
            "signature_version": "s3v4",
            "addressing_style": "path",
        },
    },
    "staticfiles": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
}


@override_settings(STORAGES=_S3_TEST_STORAGE, DIRECT_UPLOAD_ENABLED=True)
class PresignUploadTests(TestCase):
    """presign_upload hands out a signed PUT URL for a server-chosen, per-user key."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="intruder", password="pw")
        self.client.force_login(self.user)

    def _presign(self, url_name, *args, filename="movie.mp4"):
        return self.client.post(
            reverse(url_name, args=args),
            {"filename": filename},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_returns_signed_url_and_namespaced_key(self):
        resp = self._presign("presign_upload_root")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Key is minted server-side under the user's own prefix; the client's
        # filename only contributes a sanitized basename.
        self.assertTrue(data["key"].startswith(f"user_{self.user.id}/"))
        self.assertTrue(data["key"].endswith("movie.mp4"))
        self.assertEqual(data["key"].count("/"), 2)
        self.assertIn("X-Amz-Signature", data["url"])

    def test_scopes_target_folder_to_owner(self):
        theirs = Folder.objects.create(name="theirs", owner=self.other)
        resp = self._presign("presign_upload", theirs.id)
        self.assertEqual(resp.status_code, 404)

    def test_requires_login(self):
        self.client.logout()
        resp = self._presign("presign_upload_root")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_disabled_returns_404(self):
        with override_settings(DIRECT_UPLOAD_ENABLED=False):
            self.assertEqual(self._presign("presign_upload_root").status_code, 404)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "files.tests.RemoteLikeStorage"},
        "staticfiles": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    },
    DIRECT_UPLOAD_ENABLED=True,
)
class CommitUploadTests(TestCase):
    """commit_upload records a directly-uploaded file, failing closed on any
    key it didn't mint or any object that isn't really there."""

    def setUp(self):
        RemoteLikeStorage._files = {}
        User = get_user_model()
        self.user = User.objects.create_user(username="owner", password="pw")
        self.client.force_login(self.user)
        self.key = f"user_{self.user.id}/{uuid.uuid4().hex}/clip.mp4"

    def _commit(self, **fields):
        return self.client.post(
            reverse("commit_upload_root"),
            fields,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_records_document_with_size_read_from_bucket(self):
        RemoteLikeStorage._files[self.key] = b"hello bucket"
        # Client also sends a bogus "size"; it must be ignored in favor of the
        # bucket's real size, and the subfolder tree rebuilt from the path.
        resp = self._commit(key=self.key, path="Videos/clip.mp4", size="999999")
        self.assertEqual(resp.status_code, 200)

        doc = Document.objects.get()
        self.assertEqual(doc.size, len(b"hello bucket"))
        self.assertEqual(doc.name, "clip.mp4")
        self.assertEqual(doc.folder.name, "Videos")
        self.assertEqual(doc.file.name, self.key)

    def test_rejects_key_outside_user_prefix(self):
        evil = f"user_{self.user.id + 999}/{uuid.uuid4().hex}/secret"
        RemoteLikeStorage._files[evil] = b"someone elses object"
        resp = self._commit(key=evil, path="secret")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_rejects_malformed_key_shape(self):
        # Right prefix but extra path segments (count of '/' != 2) is refused.
        bad = f"user_{self.user.id}/a/b/c"
        RemoteLikeStorage._files[bad] = b"x"
        resp = self._commit(key=bad, path="c")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Document.objects.count(), 0)

    def test_fails_closed_when_object_not_uploaded(self):
        # Well-formed key for this user, but nothing was ever PUT to the bucket.
        resp = self._commit(key=self.key, path="clip.mp4")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Document.objects.count(), 0)

    def test_requires_login(self):
        self.client.logout()
        resp = self._commit(key=self.key, path="clip.mp4")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_disabled_returns_404(self):
        RemoteLikeStorage._files[self.key] = b"data"
        with override_settings(DIRECT_UPLOAD_ENABLED=False):
            resp = self._commit(key=self.key, path="clip.mp4")
            self.assertEqual(resp.status_code, 404)


def _jpeg_bytes(color="red", size=(24, 24)):
    """A real (tiny) JPEG, so Pillow can actually decode/thumbnail it in tests."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class ThumbnailTests(TestCase):
    """The browse grid's thumbnail endpoint: owner-scoped, returns a cached JPEG."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.doc = Document.objects.create(
            name="photo.jpg",
            file=SimpleUploadedFile("photo.jpg", _jpeg_bytes(), content_type="image/jpeg"),
            content_type="image/jpeg",
            owner=self.owner,
        )

    def test_thumbnail_returns_jpeg_and_caches(self):
        from files.views import _thumb_key

        self.client.login(username="owner", password="pw")
        url = reverse("thumbnail_document", args=[self.doc.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/jpeg")
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        # The thumbnail was cached under its derived key, and a second request
        # serves it again without error.
        from django.core.files.storage import default_storage

        self.assertTrue(default_storage.exists(_thumb_key(self.doc)))
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_thumbnail_is_owner_scoped(self):
        self.client.login(username="other", password="pw")
        resp = self.client.get(reverse("thumbnail_document", args=[self.doc.id]))
        self.assertEqual(resp.status_code, 404)


class RemoveLinkPasswordTests(TestCase):
    """One-click removal of a share link's password (owner-scoped, POST-only)."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.folder = Folder.objects.create(name="F", owner=self.owner)
        self.link = ShareLink.objects.create(folder=self.folder, created_by=self.owner)
        self.link.set_password("secret")
        self.link.save()

    def test_owner_can_remove_password(self):
        self.assertTrue(self.link.requires_password)
        self.client.login(username="owner", password="pw")
        resp = self.client.post(reverse("remove_link_password", args=[self.link.token]))
        self.assertEqual(resp.status_code, 302)
        self.link.refresh_from_db()
        self.assertFalse(self.link.requires_password)

    def test_non_owner_cannot_remove_password(self):
        self.client.login(username="other", password="pw")
        resp = self.client.post(reverse("remove_link_password", args=[self.link.token]))
        self.assertEqual(resp.status_code, 404)
        self.link.refresh_from_db()
        self.assertTrue(self.link.requires_password)

    def test_get_does_not_change_password(self):
        self.client.login(username="owner", password="pw")
        resp = self.client.get(reverse("remove_link_password", args=[self.link.token]))
        self.assertEqual(resp.status_code, 302)
        self.link.refresh_from_db()
        self.assertTrue(self.link.requires_password)


from django.core import mail


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailShareLinkTests(TestCase):
    """create_share emails the link when an email address is supplied."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.folder = Folder.objects.create(name="Trip", owner=self.owner)
        self.client.login(username="owner", password="pw")

    def test_email_sent_with_link(self):
        mail.outbox = []
        resp = self.client.post(
            reverse("create_share", args=["folder", self.folder.id]),
            {"email": "friend@example.com"},
        )
        self.assertEqual(resp.status_code, 302)
        link = ShareLink.objects.get(folder=self.folder)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["friend@example.com"])
        self.assertIn(str(link.token), msg.body)

    def test_no_email_field_sends_nothing(self):
        mail.outbox = []
        resp = self.client.post(
            reverse("create_share", args=["folder", self.folder.id]), {}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(ShareLink.objects.filter(folder=self.folder).exists())

    def test_invalid_email_does_not_create_link(self):
        mail.outbox = []
        resp = self.client.post(
            reverse("create_share", args=["folder", self.folder.id]),
            {"email": "not-an-email"},
        )
        self.assertEqual(resp.status_code, 302)
        # Form-invalid path fails closed: no link, no mail.
        self.assertFalse(ShareLink.objects.filter(folder=self.folder).exists())
        self.assertEqual(len(mail.outbox), 0)


class BrowsePaginationTests(TestCase):
    """The file browser paginates documents at 25 per page."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        for i in range(30):
            Document.objects.create(
                name=f"f{i}.txt",
                file=SimpleUploadedFile(f"f{i}.txt", b"x"),
                owner=self.owner,
            )
        self.client.login(username="owner", password="pw")

    def test_first_page_has_25(self):
        resp = self.client.get(reverse("browse"))
        self.assertEqual(len(resp.context["documents"]), 25)
        self.assertTrue(resp.context["page_obj"].has_next())

    def test_second_page_has_remainder(self):
        resp = self.client.get(reverse("browse") + "?page=2")
        self.assertEqual(len(resp.context["documents"]), 5)
        self.assertFalse(resp.context["page_obj"].has_next())


def _zip_bytes(files):
    """A real in-memory zip of {name: bytes} for zip-preview tests."""
    import io
    import zipfile as zf
    buf = io.BytesIO()
    with zf.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class ZipPreviewTests(TestCase):
    """Owners can list a zip's contents and open individual entries safely."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        body = _zip_bytes({"a.txt": b"hello", "pics/b.jpg": b"\xff\xd8\xff stub"})
        self.doc = Document.objects.create(
            name="bundle.zip",
            file=SimpleUploadedFile("bundle.zip", body, content_type="application/zip"),
            content_type="application/zip",
            size=len(body),
            owner=self.owner,
        )

    def test_preview_lists_entries(self):
        self.client.login(username="owner", password="pw")
        resp = self.client.get(reverse("preview_document", args=[self.doc.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["is_zip"])
        names = {e["name"] for e in resp.context["zip_entries"]}
        self.assertEqual(names, {"a.txt", "pics/b.jpg"})

    def test_open_entry_streams_content(self):
        self.client.login(username="owner", password="pw")
        url = reverse("zip_entry", args=[self.doc.id]) + "?path=a.txt"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"hello")
        # text is an allowlisted inline type
        self.assertTrue(resp["Content-Type"].startswith("text/plain"))
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")

    def test_entry_not_in_zip_404s(self):
        self.client.login(username="owner", password="pw")
        url = reverse("zip_entry", args=[self.doc.id]) + "?path=../secret"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_zip_entry_is_owner_scoped(self):
        self.client.login(username="other", password="pw")
        url = reverse("zip_entry", args=[self.doc.id]) + "?path=a.txt"
        self.assertEqual(self.client.get(url).status_code, 404)


class OwnerFolderZipTests(TestCase):
    """Owners can stream one of their own folders as a single zip."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.folder = Folder.objects.create(name="Album", owner=self.owner)
        sub = Folder.objects.create(name="Sub", owner=self.owner, parent=self.folder)
        Document.objects.create(
            name="a.txt", file=SimpleUploadedFile("a.txt", b"aaa"),
            folder=self.folder, owner=self.owner,
        )
        Document.objects.create(
            name="b.txt", file=SimpleUploadedFile("b.txt", b"bbb"),
            folder=sub, owner=self.owner,
        )

    def test_owner_downloads_folder_zip(self):
        self.client.login(username="owner", password="pw")
        resp = self.client.get(reverse("download_folder_zip", args=[self.folder.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertIn("Album.zip", resp["Content-Disposition"])
        body = b"".join(resp.streaming_content)
        self.assertTrue(body.startswith(b"PK"))  # valid zip

    def test_folder_zip_is_owner_scoped(self):
        self.client.login(username="other", password="pw")
        resp = self.client.get(reverse("download_folder_zip", args=[self.folder.id]))
        self.assertEqual(resp.status_code, 404)


class ZipGalleryTests(TestCase):
    """The zip preview surfaces image entries and serves per-entry thumbnails."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        body = _zip_bytes({
            "photo1.jpg": _jpeg_bytes("red"),
            "sub/photo2.png": _jpeg_bytes("blue"),  # PNG ext, JPEG bytes -> Pillow still decodes
            "notes.txt": b"hello",
        })
        self.doc = Document.objects.create(
            name="album.zip",
            file=SimpleUploadedFile("album.zip", body, content_type="application/zip"),
            content_type="application/zip", size=len(body), owner=self.owner,
        )

    def test_gallery_lists_image_entries(self):
        self.client.login(username="owner", password="pw")
        resp = self.client.get(reverse("preview_document", args=[self.doc.id]))
        self.assertEqual(resp.status_code, 200)
        gallery = {e["name"] for e in resp.context["zip_gallery"]}
        self.assertIn("photo1.jpg", gallery)
        self.assertNotIn("notes.txt", gallery)

    def test_zip_thumbnail_returns_jpeg(self):
        self.client.login(username="owner", password="pw")
        url = reverse("zip_thumbnail", args=[self.doc.id]) + "?path=photo1.jpg"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/jpeg")

    def test_zip_thumbnail_rejects_non_image(self):
        self.client.login(username="owner", password="pw")
        url = reverse("zip_thumbnail", args=[self.doc.id]) + "?path=notes.txt"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_zip_thumbnail_owner_scoped(self):
        self.client.login(username="other", password="pw")
        url = reverse("zip_thumbnail", args=[self.doc.id]) + "?path=photo1.jpg"
        self.assertEqual(self.client.get(url).status_code, 404)


from files.models import EmailSettings


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailSettingsUITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(username="staff", password="pw", is_staff=True, email="staff@example.com")
        self.plain = User.objects.create_user(username="plain", password="pw")

    def test_non_staff_forbidden(self):
        self.client.login(username="plain", password="pw")
        self.assertEqual(self.client.get(reverse("email_settings")).status_code, 403)

    def test_staff_can_open_and_save(self):
        self.client.login(username="staff", password="pw")
        self.assertEqual(self.client.get(reverse("email_settings")).status_code, 200)
        resp = self.client.post(reverse("email_settings"), {
            "enabled": "on", "host": "smtp.gmail.com", "port": "587",
            "username": "me@gmail.com", "password": "apppassword1234",
            "use_tls": "on", "from_email": "me@gmail.com",
        })
        self.assertEqual(resp.status_code, 302)
        cfg = EmailSettings.load()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.host, "smtp.gmail.com")
        self.assertEqual(cfg.password, "apppassword1234")

    def test_blank_password_keeps_existing(self):
        EmailSettings.objects.create(pk=1, host="smtp.gmail.com", password="secret123", username="me@gmail.com")
        self.client.login(username="staff", password="pw")
        self.client.post(reverse("email_settings"), {
            "enabled": "", "host": "smtp.gmail.com", "port": "587",
            "username": "me@gmail.com", "password": "", "use_tls": "on", "from_email": "",
        })
        self.assertEqual(EmailSettings.load().password, "secret123")

    def test_send_test_email(self):
        from django.core import mail
        mail.outbox = []
        self.client.login(username="staff", password="pw")
        resp = self.client.post(reverse("send_test_email"), {"to": "dest@example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["dest@example.com"])

    def test_test_email_non_staff_forbidden(self):
        self.client.login(username="plain", password="pw")
        self.assertEqual(self.client.post(reverse("send_test_email"), {"to": "x@y.com"}).status_code, 403)


from files.permissions import is_admin


class IsAdminTests(TestCase):
    def test_first_real_user_is_admin_without_staff(self):
        User = get_user_model()
        owner = User.objects.create_user(username="owner", password="pw")  # first
        later = User.objects.create_user(username="later", password="pw")  # signup
        self.assertTrue(is_admin(owner))
        self.assertFalse(is_admin(later))

    def test_staff_is_admin(self):
        User = get_user_model()
        User.objects.create_user(username="owner", password="pw")
        staffer = User.objects.create_user(username="s", password="pw", is_staff=True)
        self.assertTrue(is_admin(staffer))


class ShareZipPreviewTests(TestCase):
    """A shared zip exposes its own contents (gallery + entries) but nothing else."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        body = _zip_bytes({"a.jpg": _jpeg_bytes("red"), "n.txt": b"hi"})
        self.zip_doc = Document.objects.create(
            name="b.zip",
            file=SimpleUploadedFile("b.zip", body, content_type="application/zip"),
            content_type="application/zip", size=len(body), owner=self.owner,
        )
        self.other_doc = Document.objects.create(
            name="secret.txt", file=SimpleUploadedFile("secret.txt", b"x"), owner=self.owner,
        )
        self.link = ShareLink.objects.create(document=self.zip_doc, created_by=self.owner)

    def test_share_view_lists_zip_contents(self):
        resp = self.client.get(reverse("share_view", args=[self.link.token]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["is_zip"])
        names = {e["name"] for e in resp.context["zip_entries"]}
        self.assertEqual(names, {"a.jpg", "n.txt"})

    def test_share_zip_entry_streams(self):
        url = reverse("share_zip_entry", args=[self.link.token, self.zip_doc.id]) + "?path=n.txt"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"hi")

    def test_share_zip_thumbnail(self):
        url = reverse("share_zip_thumbnail", args=[self.link.token, self.zip_doc.id]) + "?path=a.jpg"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/jpeg")

    def test_cannot_reach_other_doc_through_link(self):
        # The link targets the zip doc; another doc id must not be reachable.
        url = reverse("share_zip_entry", args=[self.link.token, self.other_doc.id]) + "?path=n.txt"
        self.assertEqual(self.client.get(url).status_code, 404)
