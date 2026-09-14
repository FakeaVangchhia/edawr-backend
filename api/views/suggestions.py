"""The console's read of the poll answers.

Read-only on purpose: a suggestion is what a customer said, and the only
honest thing a console can do with it is read it. There is no edit, no delete
and no reply — the phone number on a signed-in answer is how the store follows
up, on the phone.

The write side is `views/store.py::SuggestionCreateView`, public and throttled;
this module is the admin half of the same table.
"""

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from api.models import Suggestion
from api.paging import read_page
from api.permissions import AdminAPIView
from api.serializers import SuggestionSerializer


class SuggestionListView(AdminAPIView):
    @extend_schema(responses=SuggestionSerializer(many=True))
    def get(self, request):
        """GET /api/suggestions?limit=&offset= — newest first, either role."""
        suggestions = Suggestion.objects.select_related("customer").order_by("-created_at", "-id")

        total = suggestions.count()
        limit, offset = read_page(request, default=50, maximum=200)
        page = suggestions[offset : offset + limit]

        response = Response(SuggestionSerializer(page, many=True).data)
        response["X-Total-Count"] = str(total)
        return response
