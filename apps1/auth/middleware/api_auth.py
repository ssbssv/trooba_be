from django.http import JsonResponse


class ApiAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response


    def _handle_admin_request(self, request):
        from apps.admin.auth import is_admin_session

        admin_public = {
            "/api/admin/login/",
            "/api/admin/login",
        }

        if request.path in admin_public:
            return None

        if not is_admin_session(request):
            return JsonResponse({"error": "Unauthorized"}, status=401)

        return None

    def _handle_portal_request(self, request):
        public_paths = {
            "/api/login/",
            "/api/signup/",
            "/api/csrf/",
            "/api/organizations/invites/accept/",
            "/api/organizations/invites/accept",
            "/api/organizations/invites/preview/",
            "/api/organizations/invites/preview",
        }

        if request.path in public_paths:
            return None

        if not request.user.is_authenticated:
            return JsonResponse({"error": "Unauthorized"}, status=401)

        from apps.users.access import portal_access_block_reason

        block = portal_access_block_reason(request.user)
        if block:
            return JsonResponse(
                {"error": block, "code": "account_frozen"},
                status=403,
            )

        if request.path != "/api/profile/password/":
            try:
                if getattr(request.user.profile, "must_change_password", False):
                    return JsonResponse(
                        {
                            "error": "Password change required",
                            "code": "must_change_password",
                        },
                        status=403,
                    )
            except Exception:
                pass

        return None
    def __call__(self, request):

        if not request.path.startswith("/api/"):
            return self.get_response(request)

        if request.path.startswith("/api/auth/"):
            return self.get_response(request)

        if request.path.startswith("/api/admin/"):
            response = self._handle_admin_request(request)
            if response:
                return response
            return self.get_response(request)

        response = self._handle_portal_request(request)
        if response:
            return response

        return self.get_response(request)