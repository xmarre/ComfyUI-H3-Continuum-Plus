"""Exact source-frame masters and explicit display-referred SDR derivatives.

The one-frame FFV1 master retains decoded sample depth/color tags. It is not a
Dolby Vision archive: the original registered source remains the RPU/EL master.
"""
from __future__ import annotations

from fractions import Fraction
import math
from pathlib import Path
import subprocess
import uuid

from .media import MEDIA_FORMATS, probe, run_process
from .model import rational, digest

COLOR_VERSION = 4


def limited_dimensions(width, height, megapixels):
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise ValueError("Invalid source dimensions")
    if isinstance(megapixels, bool) or not math.isfinite(float(megapixels)) or not .01 <= float(megapixels) <= 32:
        raise ValueError("Reference limit must be between 0.01 and 32 MP")
    scale = min(1, math.sqrt(float(megapixels)*1_000_000/(width*height)))
    return max(1, math.floor(width*scale)), max(1, math.floor(height*scale))


def native_reference_dimensions(width, height, output_width, output_height, mode):
    """Fit H3's 32px lattice downward, preventing Core's rounding upscale."""
    if min(width, height) < 32:
        raise ValueError("Scene reference is smaller than the native 32-pixel lattice")
    scale = min(1., math.sqrt(output_width*output_height/(width*height))) if mode == "Match Output" else min(1., 2048/min(width, height))
    return max(32, int(width*scale)//32*32), max(32, int(height*scale)//32*32)


def color_metadata(video):
    side = video.get("side_data_list", [])
    dovi = next((s for s in side if "dovi" in s.get("side_data_type", "").lower() or "dolby vision" in s.get("side_data_type", "").lower()), None)
    return {"primaries": video.get("color_primaries", "unknown"),
            "transfer": video.get("color_transfer", "unknown"),
            "matrix": video.get("color_space", "unknown"),
            "range": video.get("color_range", "unknown"),
            "pixel_format": video.get("pix_fmt"), "side_data": side, "dolby_vision": dovi}


def color_filter(metadata, settings):
    trc, prim, matrix = metadata["transfer"], metadata["primaries"], metadata["matrix"]
    warnings = []
    dovi = metadata["dolby_vision"]
    mode = settings["tone_mapping"]
    if dovi:
        profile = int(dovi.get("dv_profile", -1))
        compatibility = int(dovi.get("dv_bl_signal_compatibility_id", 0))
        if mode == "libplacebo":
            # Applied directly to the original decoder output, before the RPU
            # could be lost in the archival derivative.
            warnings.append("Dolby Vision RPU processing depends on FFmpeg/libplacebo and profile support; enhancement-layer reconstruction is not provided.")
        elif profile == 7 or (profile == 8 and compatibility in {1, 2, 4}):
            warnings.append(f"Dolby Vision profile {profile}: compatible base layer only; dynamic RPU/enhancement layer not applied.")
        else:
            raise ValueError(f"Dolby Vision profile {profile} has no verified SDR/HDR base-layer fallback. Select libplacebo with Dolby Vision support or supply a color-managed SDR source.")
    if mode == "libplacebo":
        return ("libplacebo=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc:"
                "tonemapping=bt.2390:apply_dolbyvision=1:peak_detect=0,format=rgb48le", warnings)
    if trc in {"unknown", "unspecified", "reserved", None}:
        trc = "bt709"
        warnings.append("Missing transfer metadata; explicit Rec.709 fallback used. Correct source tags if this is HDR.")
    if prim in {"unknown", "unspecified", "reserved", None}:
        prim = "bt709"
        warnings.append("Missing color primaries; Rec.709 fallback used.")
    if matrix in {"unknown", "unspecified", "reserved", None}:
        matrix = "gbr" if str(metadata["pixel_format"]).startswith(("rgb", "bgr", "gbr")) else "bt709"
        warnings.append("Missing matrix metadata; RGB or Rec.709 fallback used.")
    # zscale option spelling differs from ffprobe for RGB.
    matrix = "gbr" if matrix == "rgb" else matrix
    rng = {"tv": "limited", "pc": "full"}.get(metadata["range"], "full" if matrix == "gbr" else "limited")
    # Preserve ordinary SDR transfer values. Re-encoding BT.1886
    # linear light as sRGB visibly darkens shadow and midtone code values.
    if trc not in {"smpte2084", "arib-std-b67"}:
        if prim != "bt709":
            warnings.append("Out-of-gamut SDR colors are clipped; perceptual gamut mapping is not provided.")
        return (f"zscale=pin={prim}:tin={trc}:min={matrix}:rin={rng}:"
                f"p=bt709:t={trc}:m=gbr:r=full,format=gbrpf32le,"
                f"zscale=p=bt709:t={trc}:m=gbr:r=full:dither=error_diffusion,format=rgb48le", warnings)
    # HDR must be linearized before tone mapping.
    linear = f"zscale=pin={prim}:tin={trc}:min={matrix}:rin={rng}:p={prim}:t=linear:m=gbr:r=full:npl={settings['sdr_nits']},format=gbrpf32le"
    if trc in {"smpte2084", "arib-std-b67"}:
        peak = None
        for side in metadata["side_data"]:
            value = side.get("max_content") or side.get("max_luminance")
            if value is not None:
                try:
                    measured = float(rational(value))
                    if 100 <= measured <= 10000:
                        peak = measured
                        break
                except (ValueError, ZeroDivisionError):
                    pass
        if peak is None:
            peak = 1000.
            warnings.append("HDR peak metadata unavailable; explicit 1000-nit tone-mapping peak used.")
        linear += f",zscale=p=bt709,tonemap=tonemap=mobius:desat=2:peak={peak/settings['sdr_nits']:.8g}"
    return linear + ",zscale=p=bt709:t=iec61966-2-1:m=gbr:r=full:dither=error_diffusion,format=rgb48le", warnings


def _version():
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True, timeout=10)
    return out.stdout.splitlines()[0]


def extract_reference(store, source, frame, settings, *, cancel):
    """Publish a source-native master plus independent H3/VLM/preview PNGs."""
    settings = {key: settings[key] for key in ("h3_mp", "vlm_mp", "tone_mapping", "sdr_nits", "downsample")}
    file = store.assets.verify(source["id"], cancel=cancel)
    video = probe(file)["video"]
    metadata = color_metadata(video)
    filters, warnings = color_filter(metadata, settings)
    width, height = int(video["width"]), int(video["height"])
    sar = rational(video.get("sample_aspect_ratio", "1:1").replace(":", "/")) if video.get("sample_aspect_ratio") not in {None, "N/A", "0:1"} else Fraction(1)
    display_width = max(1, round(width*sar))
    # The reference screenshot retains source raster dimensions. Its only
    # unavoidable conversion is display-referred RGB -> high-quality JPEG;
    # downstream Image Conveyor/Media Resolution owns any geometry change.
    hw, hh = width, height
    vw, vh = limited_dimensions(display_width, height, settings["vlm_mp"])
    when = Fraction(frame["pts"])*rational(frame["time_base"])
    identity = digest([COLOR_VERSION, source["id"], frame["pts"], frame["time_base"], settings, _version(), metadata])
    root = store.root / "scene_references" / identity
    manifest = root / "reference.json"
    if manifest.is_file():
        import json
        result = json.loads(manifest.read_text(encoding="utf-8"))
        for asset in result["assets"]:
            store.assets.verify(asset["id"], cancel=cancel)
        if (root / "master.mkv").is_file():
            return {**result, "cache_hit": True}
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / uuid.uuid4().hex
    temporary.mkdir()
    # copyts + explicit settb/pts selection prevents average-fps approximations
    # on VFR or nonzero-origin sources. Seek one second before, never from zero.
    select = f"settb=expr={frame['time_base']},select=eq(pts\\,{frame['pts']})"
    seek = max(rational(video.get("start_time", 0)), when-1)
    common = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1",
              "-copyts", "-seek_timestamp", "1", "-ss", f"{float(seek):.12f}",
              "-protocol_whitelist", "file,pipe", "-format_whitelist", MEDIA_FORMATS, "-i", str(file)]
    master = temporary / "master.mkv"
    tags = []
    for option, value in [("color_primaries", metadata["primaries"]), ("color_trc", metadata["transfer"]),
                          ("colorspace", metadata["matrix"]), ("color_range", metadata["range"])]:
        if value not in {None, "unknown", "unspecified", "reserved"}:
            tags += ["-"+option, value]
    try:
        cancel()
        run_process(common + ["-map", "0:v:0", "-an", "-vf", select, "-frames:v", "1", "-c:v", "ffv1",
                              "-level", "3", "-pix_fmt", video["pix_fmt"], *tags, str(master)], cancel=cancel)
        if not master.is_file() or master.stat().st_size < 200:
            raise ValueError("Exact source PTS could not be extracted")
        # Confirm that the selected filter produced a real decoded frame, not
        # an empty Matroska header when the decoder could not find that PTS.
        import av
        with av.open(str(master)) as check:
            if next(check.decode(video=0), None) is None:
                raise ValueError("Exact source PTS produced no archival frame")
        sdr = temporary / "sdr.png"
        if settings["tone_mapping"] == "libplacebo":
            color_input = common
            vf = select + "," + filters
        else:
            color_input = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1", "-i", str(master)]
            vf = filters
        run_process(color_input + ["-an", "-vf", vf, "-frames:v", "1", "-c:v", "png", "-pix_fmt", "rgb48be", "-threads:v", "1", str(sdr)], cancel=cancel)
        # Never rescale the screenshot stored for H3/Conveyor. Use 4:4:4 JPEG
        # at quality 95, with a separate reduced VLM inference derivative.
        from PIL import Image
        h3_jpeg = temporary / "h3.jpg"
        with Image.open(sdr) as image:
            image.convert("RGB").save(h3_jpeg, format="JPEG", quality=95, subsampling=0)
        assets = []
        with h3_jpeg.open("rb") as inp:
            asset = store.assets.register(inp, "h3.jpg", cancel=cancel)
        assets.append({**asset, "kind": "image", "representation": "h3",
                       "width": hw, "height": hh, "megapixels": hw*hh/1e6,
                       "bit_depth": 8, "encoding": "JPEG", "quality": 95,
                       "source_raster_unscaled": True})
        for name, w, h, depth in [("vlm", vw, vh, "rgb24"),
                                  ("preview", *limited_dimensions(display_width, height, 1), "rgb24"),
                                  ("sdr_master", display_width, height, "rgb48be")]:
            output = temporary / (name+".png")
            run_process(["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1",
                         "-i", str(sdr), "-vf", f"scale={w}:{h}:flags={settings['downsample']},setsar=1,format={depth}",
                         "-frames:v", "1", "-threads:v", "1", str(output)], cancel=cancel)
            with output.open("rb") as inp:
                asset = store.assets.register(inp, name+".png", cancel=cancel)
            assets.append({**asset, "kind": "image", "representation": name,
                           "width": w, "height": h, "megapixels": w*h/1e6, "bit_depth": 16 if name == "sdr_master" else 8})
        cancel()
        master.replace(root / "master.mkv")
        result = {"id": identity, "source_asset_id": source["id"], "pts": frame["pts"],
                  "time_base": frame["time_base"], "source_dimensions": [width, height],
                  "display_dimensions": [display_width, height], "color": metadata,
                  "color_version": COLOR_VERSION, "ffmpeg": _version(), "settings": settings,
                  "warnings": warnings, "assets": assets, "cache_hit": False,
                  "master": "decoded FFV1 samples; original source retains Dolby Vision RPU/EL"}
        store.atomic_json(manifest, result)
        return result
    finally:
        import shutil
        shutil.rmtree(temporary, ignore_errors=True)
