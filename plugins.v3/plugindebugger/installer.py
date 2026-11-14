import ast
import base64
import binascii
import io
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_SKIP_PARTS = ("__MACOSX", ".git", ".github", "__pycache__", ".idea", ".vscode")


def safe_name(name: Any) -> str:
    raw = str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    raw = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", raw)
    return raw[:120]


def decode_upload(data: Any, max_bytes: int) -> Tuple[bytes, str]:
    if not isinstance(data, str) or not data.strip():
        return b"", "没有收到文件内容"
    raw = data.strip()
    if "," in raw[:80] and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return b"", "文件内容不是合法的 base64，重新选一次 ZIP"
    if not content:
        return b"", "文件是空的"
    if len(content) > max_bytes:
        return b"", f"文件 {len(content) // 1048576} MB 超过设置里的上限"
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return b"", "这不是一个能打开的 ZIP 包"
    return content, ""


def _clean_rel(rel: str) -> str:
    return rel.replace("\\", "/").lstrip("/")


def _safe_rel(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if any(p in _SKIP_PARTS or p.startswith(".") for p in parts):
        return False
    return True


def read_init(raw: bytes, member: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return zf.read(member).decode("utf-8", errors="replace")
    except Exception:
        return ""


def plugin_meta(source: str, fallback: str) -> Dict[str, str]:
    out = {"id": "", "name": "", "version": ""}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = []
            for base in node.bases:
                bases.append(base.id if isinstance(base, ast.Name) else
                             base.attr if isinstance(base, ast.Attribute) else "")
            if not any("PluginBase" in b for b in bases):
                continue
            out["id"] = node.name
            keys = {"plugin_name": "name", "plugin_version": "version"}
            for item in node.body:
                if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                value = item.value
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in keys:
                        out[keys[target.id]] = value.value
            break
    if not out["id"]:
        guess = re.sub(r"[^A-Za-z0-9_]", "", fallback).strip("_")
        guess = guess[:1].upper() + guess[1:] if guess else ""
        if _ID_RE.match(guess or ""):
            out["id"] = guess
    return out


def inspect_zip(raw: bytes, prefer: str = "") -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": "", "candidates": [],
                              "prefix": "", "id": "", "name": "", "version": "", "files": 0}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
    except Exception as err:
        result["error"] = f"ZIP 打不开：{type(err).__name__}"
        return result
    inits = sorted(n for n in names if n.endswith("/__init__.py") or n == "__init__.py")
    inits = [n for n in inits if _safe_rel(_clean_rel(n))]
    if not inits:
        result["error"] = "ZIP 里没找到 __init__.py，插件包至少要有一个插件目录"
        return result
    prefixes = sorted({n.rsplit("/", 1)[0] + "/" if "/" in n else "" for n in inits},
                      key=lambda p: (p.count("/"), len(p)))
    prefix = ""
    if prefer:
        wanted = _clean_rel(prefer).strip("/") + "/"
        prefix = next((p for p in prefixes if p.rstrip("/") == wanted.rstrip("/")), "")
        if not prefix:
            result["error"] = f"没找到你指定的目录 {prefer}，可选：{'、'.join(p.rstrip('/') or '(根目录)' for p in prefixes)}"
            result["candidates"] = [p.rstrip("/") for p in prefixes]
            return result
    else:
        prefix = prefixes[0]
    chosen = next(n for n in inits if (n.rsplit("/", 1)[0] + "/" if "/" in n else "") == prefix)
    meta = plugin_meta(read_init(raw, chosen), prefix.rstrip("/").rsplit("/", 1)[-1])
    if not meta["id"]:
        result["error"] = "读不出插件主类名，确认 __init__.py 里有继承 _PluginBase 的类"
        result["candidates"] = [p.rstrip("/") for p in prefixes]
        return result
    members = [n for n in names if n.startswith(prefix) and _safe_rel(_clean_rel(n[len(prefix):]))]
    result.update(ok=True, candidates=[p.rstrip("/") for p in prefixes], prefix=prefix,
                  id=meta["id"], name=meta["name"], version=meta["version"], files=len(members))
    return result


def extract_plugin(raw: bytes, prefix: str, dest: Path) -> Tuple[int, str]:
    try:
        stage = Path(tempfile.mkdtemp(prefix="pdg-stage-"))
    except Exception as err:
        return 0, f"临时目录建不起来：{type(err).__name__}"
    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                name = _clean_rel(info.filename)
                if info.is_dir() or not name.startswith(prefix):
                    continue
                rel = name[len(prefix):]
                if not rel or not _safe_rel(rel):
                    continue
                target = (stage / rel).resolve()
                if not str(target).startswith(str(stage.resolve())):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
        if not count or not (stage / "__init__.py").is_file():
            shutil.rmtree(stage, ignore_errors=True)
            return 0, "解压后没有 __init__.py，包结构不对"
        dest.parent.mkdir(parents=True, exist_ok=True)
        old = None
        if dest.exists():
            old = dest.parent / (dest.name + ".pdg-old")
            shutil.rmtree(old, ignore_errors=True)
            dest.rename(old)
        try:
            shutil.move(str(stage), str(dest))
        except Exception:
            if old and old.exists() and not dest.exists():
                old.rename(dest)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return count, ""
    except Exception as err:
        shutil.rmtree(stage, ignore_errors=True)
        return 0, f"解压写入失败：{type(err).__name__}: {err}"


def make_backup(src: Path, dst: Path) -> bool:
    if not src.is_dir():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in src.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(src))
        return True
    except Exception:
        return False


def restore_backup(archive: Path, dest: Path) -> Tuple[bool, str]:
    if not archive.is_file():
        return False, "备份文件不在了"
    try:
        stage = Path(tempfile.mkdtemp(prefix="pdg-restore-"))
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                rel = _clean_rel(info.filename)
                if info.is_dir() or not rel or not _safe_rel(rel):
                    continue
                target = (stage / rel).resolve()
                if not str(target).startswith(str(stage.resolve())):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        if not (stage / "__init__.py").is_file():
            shutil.rmtree(stage, ignore_errors=True)
            return False, "备份里没有 __init__.py"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(stage, dest)
        shutil.rmtree(stage, ignore_errors=True)
        return True, ""
    except Exception as err:
        return False, f"恢复写入失败：{type(err).__name__}: {err}"
