from django.contrib.auth import get_user_model

# The auto-created account used by the anonymous "quick share" upload path; it is
# never the human owner, so it's excluded from the first-user fallback below.
ANON_USERNAME = "anonymous"


def is_admin(user):
    """Who may manage app-wide settings (e.g. email).

    Staff/superusers always qualify. As a fallback for a single-owner deploy
    where nobody set the staff flag, the earliest registered real account (the
    owner) qualifies too -- so the owner can reach settings without a shell,
    while a later public signup (higher id) cannot.
    """
    if user is None or not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    first = (
        get_user_model()
        .objects.exclude(username=ANON_USERNAME)
        .order_by("id")
        .first()
    )
    return first is not None and first.id == user.id
