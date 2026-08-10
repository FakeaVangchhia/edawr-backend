"""Teaching drf-spectacular about this project's custom auth.

drf-spectacular builds the OpenAPI document behind `/docs` by introspecting
views and serializers. It recognises DRF's built-in authentication classes, but
`AdminJWTAuthentication` is ours, so without this extension it logs
"could not resolve authenticator" and the Swagger UI has no **Authorize**
button — which makes the admin endpoints untestable from the browser.

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
