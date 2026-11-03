"""Call these helpers only after resolving the attachment through its authorized owner."""
from pathlib import Path

from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.cache import never_cache

from .backends import StorageError
from .models import PreviewAsset
from .previews import RENDERER, prepare_preview, queue_preview
from .services import file_response


@never_cache
def preview_status(request, source, can_retry=False):
    if request.method not in {"GET", "POST"}:
        return HttpResponseNotAllowed(["GET", "POST"])
    if request.method == "POST" and (not can_retry or request.POST.get("action") != "retry"):
        return JsonResponse({"error": "无权执行此操作。"}, status=404)
    suffix = Path(source.original_name).suffix.lower()
    if suffix in {".doc", ".docx", ".ppt", ".pptx"}:
        asset = queue_preview(source, retry=request.method == "POST")
    else:
        asset = prepare_preview(source, retry=request.method == "POST")
    return JsonResponse({"state": asset.state, "renderer_version": asset.renderer_version,
                         "pages": asset.page_manifest if asset.state == "ready" else []})


@never_cache
def preview_file(request, source):
    if request.method not in {"GET", "HEAD"}:
        return HttpResponseNotAllowed(["GET", "HEAD"])
    asset = PreviewAsset.objects.filter(source_file=source, source_sha256=source.sha256,
                                        renderer_version=RENDERER, state="ready").select_related("output_file").first()
    if source.state != "ready" or not asset or not asset.output_file_id:
        return JsonResponse({"error": "预览尚未就绪。"}, status=404)
    try:
        return file_response(asset.output_file, inline=True)
    except StorageError:
        return JsonResponse({"error": "文件服务暂时不可用。"}, status=503)
