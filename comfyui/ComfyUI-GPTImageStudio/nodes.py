"""ComfyUI nodes: GPT Image (text to image / image editing).

Talks to any OpenAI-compatible Images API with your own key, so it works with
the official endpoint as well as third-party gateways.

Install: copy this folder into ComfyUI/custom_nodes/ and restart ComfyUI.
Nodes show up under the "GPT Image Studio" category.
"""

import io
import json
import os

import numpy as np
import torch
from PIL import Image

import folder_paths

from . import client

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536", "1920x1080", "1080x1920"]
QUALITIES = ["low", "medium", "high", "auto"]
BACKGROUNDS = ["auto", "opaque", "transparent"]
FIDELITY = ["high", "low"]


def _conf():
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}


def resolve_key(field_value):
    key = (field_value or "").strip()
    if key:
        return key
    key = (os.environ.get("IMAGE_API_KEY") or "").strip()
    if key:
        return key
    key = (_conf().get("api_key") or "").strip()
    if key:
        return key
    raise RuntimeError(
        "No API key. Fill the api_key field, set IMAGE_API_KEY, or put "
        '{"api_key": "..."} into %s' % CONFIG
    )


def resolve_base(field_value):
    base = (field_value or "").strip()
    if base:
        return base
    base = (_conf().get("base_url") or "").strip()
    return base or client.DEFAULT_BASE


def resolve_model(field_value):
    model = (field_value or "").strip()
    if model:
        return model
    model = (_conf().get("model") or "").strip()
    return model or client.DEFAULT_MODEL


def tensor_to_pngs(image):
    """IMAGE ([B,H,W,C] float 0..1) -> [(filename, png bytes)]"""
    out = []
    if image is None:
        return out
    if image.dim() == 3:
        image = image.unsqueeze(0)
    for i in range(image.shape[0]):
        arr = (image[i].detach().cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
        img = Image.fromarray(arr, mode="RGBA") if arr.shape[-1] == 4 else Image.fromarray(arr[:, :, :3], mode="RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out.append(("ref%d.png" % i, buf.getvalue()))
    return out


def png_to_tensor(blob):
    img = Image.open(io.BytesIO(blob))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


class GPTImageGenerate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "", "dynamicPrompts": True}),
                "size": (SIZES, {"default": "auto"}),
                "quality": (QUALITIES, {"default": "high"}),
                "background": (BACKGROUNDS, {"default": "opaque"}),
            },
            "optional": {
                "model": ("STRING", {"default": ""}),
                "base_url": ("STRING", {"default": ""}),
                "api_key": ("STRING", {"default": "", "multiline": False}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                                 "control_after_generate": True}),
                "timeout": ("INT", {"default": 300, "min": 30, "max": 900}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("image", "file_path", "usage")
    FUNCTION = "run"
    CATEGORY = "GPT Image Studio"
    DESCRIPTION = "Text to image via an OpenAI-compatible Images API"

    def run(self, prompt, size, quality, background, model="", base_url="",
            api_key="", seed=0, timeout=300):
        if not prompt.strip():
            raise RuntimeError("prompt is empty")
        res = client.generate(
            prompt,
            resolve_key(api_key),
            base_url=resolve_base(base_url),
            model=resolve_model(model),
            size=size,
            quality=quality,
            background=None if background == "auto" else background,
            timeout=int(timeout),
        )
        meta = {"type": "generate", "prompt": prompt, "size": size, "quality": quality,
                "background": background, "usage": res["usage"],
                "elapsed": res["elapsed"], "seed": seed}
        path = client.save(folder_paths.get_output_directory(), res["image"], res["ext"], "gptimage", meta)
        return (png_to_tensor(res["image"]), path, json.dumps(res["usage"], ensure_ascii=False))

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return kwargs.get("seed", 0)


class GPTImageEdit:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "", "dynamicPrompts": True}),
                "image1": ("IMAGE",),
                "size": (SIZES, {"default": "auto"}),
                "quality": (QUALITIES, {"default": "high"}),
                "input_fidelity": (FIDELITY, {"default": "high"}),
            },
            "optional": {
                "image2": ("IMAGE",),
                "image3": ("IMAGE",),
                "image4": ("IMAGE",),
                "background": (BACKGROUNDS, {"default": "auto"}),
                "model": ("STRING", {"default": ""}),
                "base_url": ("STRING", {"default": ""}),
                "api_key": ("STRING", {"default": "", "multiline": False}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                                 "control_after_generate": True}),
                "timeout": ("INT", {"default": 300, "min": 30, "max": 900}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("image", "file_path", "usage")
    FUNCTION = "run"
    CATEGORY = "GPT Image Studio"
    DESCRIPTION = "Edit images with up to 4 reference inputs"

    def run(self, prompt, image1, size, quality, input_fidelity,
            image2=None, image3=None, image4=None, background="auto",
            model="", base_url="", api_key="", seed=0, timeout=300):
        if not prompt.strip():
            raise RuntimeError("prompt is empty")
        refs = []
        for img in (image1, image2, image3, image4):
            refs.extend(tensor_to_pngs(img))
        refs = refs[:16]
        res = client.edit(
            prompt,
            refs,
            resolve_key(api_key),
            base_url=resolve_base(base_url),
            model=resolve_model(model),
            size=size,
            quality=quality,
            background=None if background == "auto" else background,
            input_fidelity=input_fidelity,
            timeout=int(timeout),
        )
        meta = {"type": "edit", "prompt": prompt, "size": size, "quality": quality,
                "input_fidelity": input_fidelity, "refs": [r[0] for r in refs],
                "usage": res["usage"], "elapsed": res["elapsed"], "seed": seed}
        path = client.save(folder_paths.get_output_directory(), res["image"], res["ext"], "gptimage", meta)
        return (png_to_tensor(res["image"]), path, json.dumps(res["usage"], ensure_ascii=False))

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return kwargs.get("seed", 0)


NODE_CLASS_MAPPINGS = {
    "GPTImageGenerate": GPTImageGenerate,
    "GPTImageEdit": GPTImageEdit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GPTImageGenerate": "GPT Image · 文生图",
    "GPTImageEdit": "GPT Image · 参考图编辑",
}
