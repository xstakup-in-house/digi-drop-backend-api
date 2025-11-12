from rest_framework import permissions



class HasPassPermission(permissions.BasePermission):
    """
    Allows access only to authenticated users who have has_pass=True in their Profile.
    """

    def has_permission(self, request, view):
        user = request.user

        # Check if user is authenticated and has a profile with has_pass=True
        return bool(
            user and user.is_authenticated and hasattr(user, "profile") and user.profile.has_pass
        )