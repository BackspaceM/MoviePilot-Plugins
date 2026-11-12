"""从种子标题里读出会影响观感的那几个标记。"""

from __future__ import annotations

import re
from typing import Dict, List

DIMENSION_LABELS = {
    "hdr": "HDR / 杜比视界",
    "codec": "编码",
    "resolution": "分辨率",
    "fps": "帧率",
    "source": "来源",
    "bitrate": "码率",
    "audio": "音轨",
}
DEFAULT_DIMENSIONS = ["hdr", "codec", "resolution", "fps", "source"]


def normalize(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[._\-\[\]()]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse(title: str) -> Dict[str, str]:
    text = normalize(title)
    fp = {key: "" for key in DIMENSION_LABELS}
    if not text:
        return fp

    if re.search(r"dolby\s*vision|杜比视界|\bdv\b|\bdovi\b", text):
        fp["hdr"] = "DV"
    elif re.search(r"hdr\s*10\s*\+|hdr10plus", text):
        fp["hdr"] = "HDR10+"
    elif re.search(r"\bhlg\b", text):
        fp["hdr"] = "HLG"
    elif re.search(r"\bhdr\b|\bhdr10\b", text):
        fp["hdr"] = "HDR"
    elif re.search(r"\bsdr\b", text):
        fp["hdr"] = "SDR"

    if re.search(r"\b(x\s*265|h\s*265|hevc)\b", text):
        fp["codec"] = "H.265"
    elif re.search(r"\b(x\s*264|h\s*264|avc)\b", text):
        fp["codec"] = "H.264"
    elif re.search(r"\bav1\b", text):
        fp["codec"] = "AV1"
    elif re.search(r"\bvp9\b", text):
        fp["codec"] = "VP9"

    if re.search(r"\b(2160p|2160i|4k|uhd)\b", text):
        fp["resolution"] = "2160p"
    elif re.search(r"\b(1080p|1080i|fhd)\b", text):
        fp["resolution"] = "1080p"
    elif re.search(r"\b720p\b", text):
        fp["resolution"] = "720p"
    elif re.search(r"\b(480p|576p|dvd)\b", text):
        fp["resolution"] = "SD"

    fps = re.search(r"\b(\d{2,3})\s*fps\b", text)
    if fps:
        fp["fps"] = f"{int(fps.group(1))}fps"
    elif re.search(r"高帧率|high\s*frame", text):
        fp["fps"] = "高帧率"

    sources = [
        (r"\bremux\b", "Remux"),
        (r"\b(bluray|blu\s*ray|bdmv|原盘)\b", "BluRay"),
        (r"\b(bdrip|brrip)\b", "BDRip"),
        (r"\bweb\s*dl\b|\bwebdl\b", "WEB-DL"),
        (r"\bweb\s*rip\b|\bwebrip\b", "WEBRip"),
        (r"\b(hdtv|pdtv)\b", "HDTV"),
        (r"\b(dvdrip|dvdscr)\b", "DVDRip"),
    ]
    for pattern, value in sources:
        if re.search(pattern, text):
            fp["source"] = value
            break

    if re.search(r"高码|高码率|hi\s*bit|high\s*bitrate", text):
        fp["bitrate"] = "高码"
    elif re.search(r"低码|低码率", text):
        fp["bitrate"] = "低码"
    if re.search(r"\b10\s*bit\b", text):
        fp["bitrate"] = f"{fp['bitrate']}·10bit" if fp["bitrate"] else "10bit"

    if re.search(r"atmos|杜比全景声", text):
        fp["audio"] = "Atmos"
    elif re.search(r"truehd", text):
        fp["audio"] = "TrueHD"
    elif re.search(r"dts\s*hd|dts\s*ma|dtsx", text):
        fp["audio"] = "DTS-HD"
    elif re.search(r"\bddp\b|e\s*ac3|dd\s*\+", text):
        fp["audio"] = "DD+"
    elif re.search(r"\baac\b", text):
        fp["audio"] = "AAC"

    return fp


def compare(baseline: Dict[str, str], actual: Dict[str, str], dimensions: List[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for dim in dimensions:
        base = str((baseline or {}).get(dim) or "")
        cur = str((actual or {}).get(dim) or "")
        label = DIMENSION_LABELS.get(dim, dim)
        if base and cur and base != cur:
            out.append({"dimension": dim, "label": label, "baseline": base,
                        "actual": cur, "level": "conflict",
                        "text": f"{label} {base} → {cur}"})
        elif base and not cur:
            out.append({"dimension": dim, "label": label, "baseline": base,
                        "actual": "", "level": "missing",
                        "text": f"{label} 未标注（基线 {base}）"})
        elif cur and not base:
            out.append({"dimension": dim, "label": label, "baseline": "",
                        "actual": cur, "level": "extra",
                        "text": f"基线未记录 {label}：{cur}"})
    return out


def brief(fp: Dict[str, str], dimensions: List[str]) -> str:
    parts = [str((fp or {}).get(dim) or "") for dim in dimensions]
    parts = [item for item in parts if item]
    return " · ".join(parts) if parts else "未识别"
