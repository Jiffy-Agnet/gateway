import time
import tomllib
from pathlib import Path

from django.http import JsonResponse

from config.settings import base

_start_time = time.monotonic()

def meta(request):
    return JsonResponse({
        "version": base.get_app_version(),
        "time": round(time.monotonic() - _start_time, 2),
    })
