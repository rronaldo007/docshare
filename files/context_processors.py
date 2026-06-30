from .permissions import is_admin


def user_flags(request):
    """Expose `is_admin` to every template (used to show admin-only nav links)."""
    user = getattr(request, "user", None)
    return {"is_admin": is_admin(user) if user is not None else False}
