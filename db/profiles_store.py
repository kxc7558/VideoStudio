# -*- coding: utf-8 -*-
"""制片资料存取（数据层）：creative_profiles.json 读写。无业务规则。"""
import json

from shared.paths import CREATIVE_PROFILES

FIELDS = {
    "scenes": ("id", "name", "place", "era", "atmosphere", "lighting", "palette", "camera"),
    "characters": ("id", "name", "identity", "appearance", "wardrobe", "behavior"),
}


def profiles() -> dict:
    """读取本地制片资料；损坏文件时回退为空，不能影响出片。"""
    try:
        data = json.loads(CREATIVE_PROFILES.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {kind: data.get(kind, []) for kind in FIELDS}
    except Exception:
        pass
    return {kind: [] for kind in FIELDS}


def clean_profiles(data: dict) -> dict:
    cleaned = {}
    for kind, fields in FIELDS.items():
        items = data.get(kind, []) if isinstance(data, dict) else []
        cleaned[kind] = [
            {field: str(item.get(field, ""))[:800] for field in fields}
            for item in items[:30] if isinstance(item, dict) and str(item.get("id", ""))
        ]
    return cleaned


def save_profiles(data: dict) -> dict:
    cleaned = clean_profiles(data)
    CREATIVE_PROFILES.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned
