from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Document, Folder, ShareLink


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
