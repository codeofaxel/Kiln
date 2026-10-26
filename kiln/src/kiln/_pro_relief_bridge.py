"""Public-Kiln -> kiln-pro bridge for one thing: the sculpted photo relief.

A photo pressed into a part is made into a relief by kiln-pro's relief
engine (https://kiln3d.com), free at every tier: a relief made for the
printer it will print on, with a preview of how it will print.  With kiln-pro installed here (Kiln's servers) the
engine runs in this process; on a signed-in plain
install the same engine answers as the served ``photo_relief`` tool, the
photo going up and the heightmap coming back.  Signed out, offline, the
servers silent or saying no, public Kiln's own floor relief carves instead
and the decoration's reply says so once (:func:`last_miss`).  Nothing is
kept on this computer but the heightmap the carve needs.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The hosted door, asked when kiln-pro is not installed here.
WIRE_TOOL = "photo_relief"
#: Where the same relief is sculpted when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.relief.engine"
#: The engine takes a few seconds; a person is waiting on a preview.
CONSULT_TIMEOUT_S: float = 60.0
#: What a served ask sends beside the photo.
_SERVED_ARGS = ("style", "canvas_mm", "depth_mm", "layer_mm", "nozzle_mm", "mode", "mask_shape", "max_px")

__all__ = ["CONSULT_TIMEOUT_S", "LOCAL_MODULE", "WIRE_TOOL", "last_miss", "sculpt"]

_lock = threading.Lock()
_last_miss: dict[str, Any] = {}


def last_miss() -> dict[str, Any] | None:
    """Why the last served ask got no relief, worded for a person, or ``None``."""
    with _lock:
        return dict(_last_miss) if _last_miss else None


def sculpt(image_path: str, work_dir: str, **spec: Any) -> dict[str, Any] | None:
    """The sculpted relief for *image_path*, or ``None`` when the floor must carve.

    *spec* is ``style``, ``canvas_mm``, ``depth_mm``, ``layer_mm``,
    ``nozzle_mm``, ``mode``, ``mask_shape`` and ``max_px``, as
    ``photo_relief`` takes them.  The answer is the dict
    ``kiln.image_to_surface.prepare_image_for_emboss`` returns, with the
    DAT written under *work_dir*.  Never raises.
    """
    try:
        from kiln_pro.relief.engine import ReliefSpec, relief_from_photo
    except ImportError:
        return _served(image_path, work_dir, spec)
    try:
        clean = {k: v for k, v in spec.items() if v is not None}
        result = relief_from_photo(image_path, work_dir, ReliefSpec(**clean))
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        logger.debug("local relief engine failed", exc_info=True)
        from kiln.served_answer import Miss

        _note(Miss("unanswered", detail=str(exc)[:200]))
        return None
    # How it was made stays in this process; a reply carries what it is.
    result.pop("diagnostics", None)
    result["relief_source"] = "engine"
    with _lock:
        _last_miss.clear()
    return result


def _served(image_path: str, work_dir: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    from kiln import served_answer, served_makes

    try:
        import kiln.server as _srv

        bearer, expired = _srv._served_bearer()
        if not bearer:
            code = served_answer.SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
            _note(served_answer.Miss("signed_out", code))
            return None
        kwargs: dict[str, Any] = {"image_path": image_path}
        kwargs.update({k: v for k, v in spec.items() if k in _SERVED_ARGS and v is not None})
        kwargs, refusal = served_makes.send_inputs(WIRE_TOOL, kwargs, {"image": "image_path"})
        if refusal is not None:
            _note(served_answer.classify_answer(refusal) or served_answer.Miss("unanswered"))
            return None
        answer = _srv._pro_api_call(WIRE_TOOL, _timeout=CONSULT_TIMEOUT_S, _asked_by_user=True, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        import kiln.server as _srv

        _note(served_answer.classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None)))
        return None
    if not (isinstance(answer, dict) and answer.get("status") == "success" and answer.get("heightmap_png_b64")):
        _note(served_answer.classify_answer(answer) or served_answer.Miss("unanswered", detail="no relief in the answer"))
        return None
    try:
        result = _bring_home(answer, image_path, work_dir)
    except Exception as exc:  # noqa: BLE001
        logger.debug("relief answer could not be written", exc_info=True)
        _note(served_answer.Miss("unanswered", detail=str(exc)[:200]))
        return None
    with _lock:
        _last_miss.clear()
    return result


def _bring_home(answer: dict[str, Any], image_path: str, work_dir: str) -> dict[str, Any]:
    """Write the served heightmap and preview beside the carve."""
    import io

    from PIL import Image

    os.makedirs(work_dir, exist_ok=True)
    stem = Path(image_path).stem
    png = base64.b64decode(answer["heightmap_png_b64"])
    im = Image.open(io.BytesIO(png))
    width, height = im.size
    values = list(im.getdata())
    peak = 65535.0 if im.mode in ("I;16", "I") else 255.0
    dat_path = os.path.abspath(os.path.join(work_dir, f"{stem}_relief.dat"))
    with open(dat_path, "w", encoding="ascii") as fh:
        for y in range(height):
            row = values[y * width:(y + 1) * width]
            fh.write(" ".join(f"{v / peak:.4f}" for v in row) + "\n")
    preview_path = None
    if answer.get("preview_png_b64"):
        preview_path = os.path.abspath(os.path.join(work_dir, f"{stem}_relief_preview.png"))
        with open(preview_path, "wb") as fh:
            fh.write(base64.b64decode(answer["preview_png_b64"]))
    return {
        "type": "heightmap",
        "dat_path": dat_path,
        "width_px": width,
        "height_px": height,
        "aspect_ratio": round(width / height, 4) if height else 1.0,
        "treatment": answer.get("treatment", "relief"),
        "read_as": None,
        "style_applied": answer.get("style_applied"),
        "levels": answer.get("levels"),
        "layer_mm": answer.get("layer_mm"),
        "depth_mm": answer.get("depth_mm"),
        "base": answer.get("base"),
        "preview_png": preview_path,
        "notes": list(answer.get("notes") or []),
        "relief_source": "served",
    }


def _note(miss: Any) -> None:
    """Keep the one sentence the decoration's reply will carry."""
    from kiln import served_answer

    text = served_answer.sentence(
        miss,
        feature="servers",
        on_the_line="A photo pressed into a part is made into a relief on Kiln's servers, for the printer it will print on",
        cannot="sculpt this photo",
        wont="carved it with the basic relief instead, a simpler one that fades out at the edge",
        safe_remedy="Keep this one, or carve it again once Kiln can reach its servers",
    )
    out: dict[str, Any] = {"code": "RELIEF_NOT_SCULPTED", "note": text, "retryable": miss.cause != "refused"}
    out.update(served_answer.fields(miss))
    with _lock:
        _last_miss.clear()
        _last_miss.update(out)
