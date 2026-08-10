"""Teaching drf-spectacular about this project's custom auth.

drf-spectacular builds the OpenAPI document behind `/docs` by introspecting
views and serializers. It recognises DRF's built-in authentication classes, but
both of ours are custom, so without these extensions it logs "could not resolve
authenticator" for every view and the Swagger UI has no **Authorize** button —
which makes the guarded endpoints untestable from the browser.

**There must be one extension per authentication class.** Both classes run on
every request, so a missing one produces that warning on *every* view in the
project, including the public ones.

An `OpenApiAuthenticationExtension` is the registration hook: name the class it
describes and return the OpenAPI security scheme for it. It is discovered by
being imported, which `api/apps.py` does in `ready()`.
"""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class AdminJWTScheme(OpenApiAuthenticationExtension):
    target_class = "api.authentication.AdminJWTAuthentication"
    name = "AdminBearerToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Paste the `access_token` returned by POST /api/auth/login.",
        }


class RiderJWTScheme(OpenApiAuthenticationExtension):
    """The rider app's token.

    A separate scheme rather than a shared one because they are genuinely
    different credentials: the tokens carry a different `typ` claim, and each
    authentication class rejects the other's. Documenting them as one bearer
    scheme would suggest an admin token works on `/api/delivery/*`, which it
    does not — that returns 403.
    """

    target_class = "api.authentication.RiderJWTAuthentication"
    name = "RiderBearerToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Paste the `access_token` returned by POST /api/auth/rider/login. "
                "Not interchangeable with an admin token."
            ),
        }
