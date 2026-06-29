from django.urls import path

from . import views

urlpatterns = [
    # Browsing
    path("", views.browse, name="browse"),
    path("folder/<int:folder_id>/", views.browse, name="browse"),

    # Folder actions
    path("folder/new/", views.create_folder, name="create_folder_root"),
    path("folder/<int:folder_id>/new/", views.create_folder, name="create_folder"),
    path("folder/<int:folder_id>/delete/", views.delete_folder, name="delete_folder"),
    path("folder/<int:folder_id>/move/", views.move_folder, name="move_folder"),

    # Document actions
    path("upload/", views.upload_document, name="upload_root"),
    path("folder/<int:folder_id>/upload/", views.upload_document, name="upload"),
    path("upload-folder/", views.upload_folder, name="upload_folder_root"),
    path("folder/<int:folder_id>/upload-folder/", views.upload_folder, name="upload_folder"),
    # Large-file chunked upload (bypasses the ~100 MB proxy body limit)
    path("upload/chunk/", views.upload_chunk, name="upload_chunk_root"),
    path("folder/<int:folder_id>/upload/chunk/", views.upload_chunk, name="upload_chunk"),
    path("upload/chunk/complete/", views.upload_chunk_complete, name="upload_chunk_complete_root"),
    path("folder/<int:folder_id>/upload/chunk/complete/", views.upload_chunk_complete, name="upload_chunk_complete"),
    # Presigned direct-to-bucket upload (object storage only; off by default)
    path("upload/presign/", views.presign_upload, name="presign_upload_root"),
    path("folder/<int:folder_id>/upload/presign/", views.presign_upload, name="presign_upload"),
    path("upload/commit/", views.commit_upload, name="commit_upload_root"),
    path("folder/<int:folder_id>/upload/commit/", views.commit_upload, name="commit_upload"),
    path("doc/<int:doc_id>/preview/", views.preview_document, name="preview_document"),
    path("doc/<int:doc_id>/inline/", views.inline_document, name="inline_document"),
    path("doc/<int:doc_id>/download/", views.download_document, name="download_document"),
    path("doc/<int:doc_id>/delete/", views.delete_document, name="delete_document"),

    # Sharing (owner side)
    path("share/<str:kind>/<int:obj_id>/", views.create_share, name="create_share"),
    path("links/", views.my_links, name="my_links"),
    path("links/<uuid:token>/revoke/", views.revoke_link, name="revoke_link"),
    path("links/<uuid:token>/remove-password/", views.remove_link_password, name="remove_link_password"),
]
