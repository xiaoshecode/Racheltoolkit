from django.utils.cache import patch_cache_control

class PrivateResponses:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith(("/api/public/", "/static/")):
            patch_cache_control(response, private=True, no_store=True)
        return response

