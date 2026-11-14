from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional, Union

from . import atomicio
from . import config
from . import indexing

TZ = timezone(timedelta(hours=8))

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def _keyed_lock(kind: str, proj: Union[dict[str, Any], str]) -> threading.Lock:
    if isinstance(proj, dict):
        key = str(proj.get("id") or proj.get("chat_path") or id(proj))
    else:
        key = str(proj)
    token = (kind, key)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(token)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[token] = lock
        return lock


def chat_lock(proj: Union[dict[str, Any], str]) -> threading.Lock:
    return _keyed_lock("chat", proj)


def project_write_lock(proj: Union[dict[str, Any], str]) -> threading.Lock:
    """Exclusive lock for apply / undo / working-drawing writes of one session."""
    return _keyed_lock("write", proj)


def data_base() -> Path:
    """Where session folders and sessions.json live.

    Uploads create the session folder under this base, so resolving a session root
    must use the exact same fallback. Otherwise an unset CAD_DATA_DIR leaves every
    session without a root and the workbench reports the folder does not exist.
    """
    return Path(config.CAD_DATA_DIR) if config.CAD_DATA_DIR else config.STUDIO_ROOT / "data"


def sessions_path() -> Path:
    return data_base() / "sessions.json"


def load_sessions() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    path = sessions_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                items = [item for item in (data.get("projects") or data.get("sessions") or []) if isinstance(item, dict) and item.get("id")]
        except Exception:
            items = []
    try:
        from . import db
        db_items = db.list_sessions()
    except Exception:
        db_items = []
    by_id = {item["id"]: item for item in items}
    for item in db_items:
        if item["id"] in by_id:
            merged = dict(by_id[item["id"]])
            merged.update({k: v for k, v in item.items() if v not in (None, "")})
            by_id[item["id"]] = merged
        else:
            by_id[item["id"]] = item
    # DB/json sessions first, newest updated first.
    merged_list = list(by_id.values())
    merged_list.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "", reverse=True)
    return merged_list


def save_session(spec: dict[str, Any]) -> None:
    path = sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    items = [s for s in load_sessions() if s.get("id") != spec["id"]]
    items.insert(0, dict(spec))
    payload = {"schema": "cad-nl-studio-sessions/v1", "projects": items}
    atomicio.write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    try:
        from . import db
        db.upsert_session(spec)
    except Exception:
        pass


def update_session(project_id: str, fields: dict[str, Any]) -> None:
    items = load_sessions()
    found = False
    for item in items:
        if item.get("id") == project_id:
            item.update(fields)
            item["updated_at"] = now_iso()
            found = True
            break
    if not found:
        # Builtin projects are not in sessions.json; still persist OSS keys etc.
        try:
            from . import db
            payload = {"id": project_id, **fields}
            payload["updated_at"] = now_iso()
            db.update_session(project_id, payload)
        except Exception:
            pass
        return
    path = sessions_path()
    atomicio.write_text_atomic(
        path, json.dumps({"schema": "cad-nl-studio-sessions/v1", "projects": items}, ensure_ascii=False, indent=2) + "\n"
    )
    try:
        from . import db
        db.update_session(project_id, fields)
    except Exception:
        pass


def _overlay_spec(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Copy non-empty fields from overlay onto base without wiping catalog paths."""
    out = dict(base)
    skip = {"root", "dxf_path", "available", "index_json", "index_md", "chat_path"}
    for key, value in overlay.items():
        if key in skip or value is None or value == "":
            continue
        out[key] = value
    return out


def merge_registry(sessions: list[dict[str, Any]], builtins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """User sessions first, then builtins. A session row must not shadow a builtin catalog entry."""
    by_id = {item["id"]: dict(item) for item in builtins if item.get("id")}
    builtin_ids = [item["id"] for item in builtins if item.get("id")]
    user_sessions: list[dict[str, Any]] = []
    for item in sessions:
        sid = item.get("id")
        if not sid:
            continue
        if sid in by_id or item.get("kind") == "builtin":
            if sid in by_id:
                by_id[sid] = _overlay_spec(by_id[sid], item)
            continue
        user_sessions.append(item)
    return user_sessions + [by_id[sid] for sid in builtin_ids]


def _load_registry() -> list[dict[str, Any]]:
    builtin = json.loads(config.PROJECTS_FILE.read_text(encoding="utf-8"))["projects"]
    return merge_registry(load_sessions(), builtin)


def _resolve_root(spec: dict[str, Any]) -> Optional[Path]:
    env_name = spec.get("root_env") or ""
    if env_name:
        override = os.getenv(env_name, "").strip()
        if override:
            return Path(override)
    subdir = spec.get("data_subdir") or ""
    if subdir:
        candidate = data_base() / subdir
        if candidate.is_dir():
            return candidate
        # An uploaded session owns its folder under data_base even before the
        # preparation step has written anything into it.
        if (spec.get("kind") or "") == "session":
            return candidate
    default = spec.get("default_root")
    if default:
        p = Path(default)
        if p.is_dir():
            return p
    return Path(default) if default else None


def list_specs() -> list[dict[str, Any]]:
    return _load_registry()


def get_spec(project_id: str) -> dict[str, Any]:
    for spec in _load_registry():
        if spec["id"] == project_id:
            return spec
    raise KeyError(project_id)


def resolve_project(project_id: str) -> dict[str, Any]:
    spec = get_spec(project_id)
    root = _resolve_root(spec)
    dxf = (root / spec["deliverable"]) if root else None
    available = bool(root and root.is_dir() and dxf and dxf.is_file())
    return {
        **spec,
        "kind": spec.get("kind") or "builtin",
        "root": root,
        "dxf_path": dxf,
        "available": available,
        "index_json": (root / "index.json") if root else None,
        "index_md": (root / "INDEX.md") if root else None,
        "chat_path": (root / "chat.json") if root else None,
    }


def list_projects() -> list[dict[str, Any]]:
    from . import init_proj

    out = []
    for spec in _load_registry():
        proj = resolve_project(spec["id"])
        init_info = None
        if proj.get("root") and Path(proj["root"]).is_dir():
            try:
                init_info = init_proj.public_status(proj)
            except Exception:
                init_info = None
        ready = bool(init_info and init_info.get("ready"))
        kind = proj.get("kind") or "builtin"
        if proj["available"]:
            missing = None
        elif kind == "session":
            missing = (init_info or {}).get("message") or "会话还在准备。"
        elif not (proj["dxf_path"] and Path(proj["dxf_path"]).is_file()):
            missing = "找不到交付 DXF，请检查项目目录和 CAD_DATA_DIR。"
        else:
            missing = "项目目录不存在。"
        # Sessions being prepared should stay clickable so the user can watch progress or switch back.
        selectable = bool(proj["available"] or kind == "session")
        out.append(
            {
                "id": proj["id"],
                "name": proj["name"],
                "kind": kind,
                "available": proj["available"],
                "selectable": selectable,
                "ready": ready,
                "init": init_info,
                "note": proj.get("note") or "",
                "default_sheet": proj.get("default_sheet"),
                "default_subview": proj.get("default_subview"),
                "deliverable": proj.get("deliverable"),
                "created_at": proj.get("created_at"),
                "updated_at": proj.get("updated_at"),
                "missing": missing,
            }
        )
    return out


def _legacy_index_path(proj: dict[str, Any]) -> Optional[Path]:
    path = proj.get("index_json")
    if path:
        path = Path(path)
        if path.is_file():
            return path
    root = proj.get("root")
    if root:
        path = Path(root) / "index.json"
        if path.is_file():
            return path
    return None


def _legacy_doc_dir(proj: dict[str, Any]) -> Optional[Path]:
    if proj.get("root"):
        return Path(proj["root"])
    path = proj.get("index_json")
    if path:
        return Path(path).parent
    return None


def load_index(proj: dict[str, Any]) -> dict[str, Any]:
    """Load the full current-state index from SQLite. Leftover index.json is imported once if needed."""
    from . import db
    pid = proj.get("id")
    data = db.load_index(pid) if pid else None
    if not data:
        path = _legacy_index_path(proj)
        if path and pid:
            data = db.import_index_file(pid, path)
        if not data and path:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except Exception:
                data = {}
    if pid:
        doc_dir = _legacy_doc_dir(proj)
        if doc_dir:
            db.import_changelog_file(pid, doc_dir)
    return data if isinstance(data, dict) else {}


def _bbox(seq: Any) -> Optional[list[float]]:
    if not seq:
        return None
    if isinstance(seq, dict) and "bbox" in seq:
        return _bbox(seq["bbox"])
    if isinstance(seq, (list, tuple)) and len(seq) >= 4:
        return [float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3])]
    return None


def _pad_point(xy: list[float], half: float) -> list[float]:
    x, y = float(xy[0]), float(xy[1])
    return [x - half, y - half, x + half, y + half]


def _room_bbox(room: dict[str, Any]) -> Optional[list[float]]:
    for key in ("bbox", "walls", "extent", "extents"):
        val = room.get(key)
        bb = _bbox(val) if not isinstance(val, dict) or "bbox" in (val or {}) else _bbox(val)
        if bb:
            return bb
        if isinstance(val, dict):
            bb = _bbox(val.get("bbox") or val.get("frame"))
            if bb:
                return bb
        # walls might be a list of points
        if key == "walls" and isinstance(val, list) and val:
            xs, ys = [], []
            for item in val:
                if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[0], (int, float)):
                    xs.append(float(item[0]))
                    ys.append(float(item[1]))
                elif isinstance(item, dict):
                    for a, b in (("x1", "y1"), ("x2", "y2")):
                        if a in item and b in item:
                            xs.append(float(item[a]))
                            ys.append(float(item[b]))
                    if "bbox" in item:
                        bb = _bbox(item["bbox"])
                        if bb:
                            return bb
            if xs and ys:
                return [min(xs), min(ys), max(xs), max(ys)]
    xy = room.get("world_xy")
    if isinstance(xy, (list, tuple)) and len(xy) >= 2:
        name = room.get("name") or ""
        half = 20000 if any(k in name for k in ("餐厅", "食堂", "大厅")) else 6000
        return _pad_point(list(xy), half)
    return None


def _sheet_regions(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    sid = sheet.get("id") or sheet.get("title") or "sheet"
    title = sheet.get("title") or sid
    floor = sheet.get("floor")
    regions = []
    frame = _bbox(sheet.get("frame"))
    if frame:
        regions.append(
            {
                "id": f"sheet:{sid}",
                "kind": "sheet",
                "sheet_id": sid,
                "title": title,
                "subtitle": "整张图框",
                "floor": floor,
                "bbox": frame,
                "scale": sheet.get("scale"),
            }
        )
    for i, sub in enumerate(sheet.get("subviews") or []):
        name = sub.get("name") or f"分区{i+1}"
        sub_frame = _bbox(sub.get("frame"))
        if not sub_frame:
            continue
        regions.append(
            {
                "id": f"subview:{sid}:{name}",
                "kind": "subview",
                "sheet_id": sid,
                "title": name,
                "subtitle": title,
                "floor": floor,
                "bbox": sub_frame,
                "scale": sub.get("scale") or sheet.get("scale"),
            }
        )
    return regions


def iter_regions(proj: dict[str, Any], index: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    index = index if index is not None else load_index(proj)
    regions: list[dict[str, Any]] = []
    sheets = index.get("sheets") or []
    for sheet in sheets:
        regions.extend(_sheet_regions(sheet))
    # rooms after sheets, so the switcher can group them
    seen = set()
    for room in index.get("rooms") or []:
        sheet_id = (room.get("sheet") or "").split(" ")[0] if room.get("sheet") else ""
        # Prefer matching a known sheet id contained in the room's sheet label
        matched = None
        for r in regions:
            if r["kind"] == "sheet" and r["sheet_id"] and r["sheet_id"] in (room.get("sheet") or ""):
                matched = r
                break
        sid = matched["sheet_id"] if matched else (sheet_id or "unknown")
        bb = _room_bbox(room)
        if not bb:
            continue
        name = room.get("name") or "房间"
        # include a qualifier from the sheet label after the first slash or the full label
        label = room.get("sheet") or sid
        key = (sid, name, round(bb[0]), round(bb[1]), label)
        if key in seen:
            continue
        seen.add(key)
        regions.append(
            {
                "id": f"room:{sid}:{name}:{round(bb[0])}:{round(bb[1])}",
                "kind": "room",
                "sheet_id": sid,
                "title": name,
                "subtitle": label,
                "floor": room.get("floor") or (matched or {}).get("floor"),
                "bbox": bb,
                "scale": None,
            }
        )
    return regions


def default_region_id(proj: dict[str, Any], regions: list[dict[str, Any]]) -> Optional[str]:
    sheet = proj.get("default_sheet")
    sub = proj.get("default_subview")
    if sheet and sub:
        wanted = f"subview:{sheet}:{sub}"
        for r in regions:
            if r["id"] == wanted:
                return r["id"]
        # fuzzy: subview title match on that sheet
        for r in regions:
            if r["kind"] == "subview" and r.get("sheet_id") == sheet and r.get("title") == sub:
                return r["id"]
    if sheet:
        wanted = f"sheet:{sheet}"
        for r in regions:
            if r["id"] == wanted:
                return r["id"]
        for r in regions:
            if r["kind"] == "sheet" and r.get("sheet_id") == sheet:
                return r["id"]
    return regions[0]["id"] if regions else None


def get_region(proj: dict[str, Any], region_id: Optional[str]) -> dict[str, Any]:
    regions = iter_regions(proj)
    if not regions:
        raise ValueError("这个项目的索引里没有可用的图幅范围。")
    rid = region_id or default_region_id(proj, regions)
    for r in regions:
        if r["id"] == rid:
            return r
    raise KeyError(region_id)


def region_label(region: dict[str, Any]) -> str:
    parts = [region.get("subtitle") or region.get("sheet_id") or "", region.get("title") or ""]
    floor = region.get("floor")
    text = " / ".join(p for p in parts if p and p not in parts[: parts.index(p)])
    # unique preserve order
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    text = " / ".join(seen)
    if floor and floor not in text:
        text = f"{text}（{floor}）"
    return text or "当前图幅"


def _tokens(text: str) -> list[str]:
    parts = re.findall(r"[\u4e00-\u9fff]{1,}|[A-Za-z0-9_$-]{2,}", text or "")
    return [p for p in parts if p not in {"一个", "一下", "我们", "这个", "那个", "以及", "或者", "还有"}]



def _is_focus_room(room: dict[str, Any], region: dict[str, Any]) -> bool:
    if region.get("kind") != "room":
        return False
    if room.get("name") != region.get("title"):
        return False
    if region.get("floor") and room.get("floor") and room.get("floor") != region.get("floor"):
        return False
    return True


def _equip_label(e: dict[str, Any]) -> str:
    name = e.get("block") or e.get("name") or "设备"
    label = e.get("display_name")
    if label and label != name:
        return f"{label}（块名 {name}）"
    return str(name)


def _equipment_lines(room: dict[str, Any], *, full: bool) -> list[str]:
    items = room.get("equipment_nearby") or []
    labels = room.get("labels_nearby") or []
    lines: list[str] = []
    if items:
        if full:
            lines.append(f"  设备（{len(items)} 条，坐标为世界坐标）：")
            for e in items:
                bit = (
                    f"  - {_equip_label(e)}  坐标={e.get('xy')}  旋转={e.get('rot')}  "
                    f"图层={e.get('layer') or ''}"
                )
                dist = e.get("dist")
                if dist is not None:
                    try:
                        bit += f"  距房间名约={round(float(dist), 1)}mm"
                    except (TypeError, ValueError):
                        pass
                if e.get("host_block"):
                    bit += f"  所在底图块={e.get('host_block')}"
                if e.get("edit_hint") == "overlay":
                    bit += "  改法=在模型空间覆盖，勿改底图块定义"
                if e.get("attribs"):
                    bit += f"  属性={e.get('attribs')}"
                lines.append(bit)
        else:
            counts = Counter(_equip_label(e) for e in items)
            summary = "、".join(f"{n}×{c}" for n, c in counts.most_common(8))
            lines.append(f"  设备 {len(items)} 条：{summary}")
    if full and labels:
        lines.append("  附近文字：")
        for lab in labels[:20]:
            if isinstance(lab, dict):
                lines.append(
                    f"  - {lab.get('text') or lab}  坐标={lab.get('xy')}  图层={lab.get('layer') or ''}"
                )
            else:
                lines.append(f"  - {lab}")
    return lines


def excerpt_index(proj: dict[str, Any], region: dict[str, Any], user_text: str, *, max_chars: int = 12000) -> str:
    """Send a tight INDEX excerpt: current sheet/rooms/related blocks, never the changelog."""
    index = load_index(proj)
    lines: list[str] = []
    meta = index.get("project") or {}
    if isinstance(meta, str):
        meta = {"name": meta}
    elif not isinstance(meta, dict):
        meta = {}
    source = index.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    lines.append(f"项目：{proj['name']}")
    if meta.get("name"):
        lines.append(f"工程：{meta.get('name')}")
    if meta.get("discipline"):
        lines.append(f"专业：{meta.get('discipline')}")
    lines.append("单位：mm")
    enc = source.get("converted_dxf_body_encoding") or proj.get("encoding")
    if enc:
        lines.append(f"编码说明：{enc}；当前交付图按 UTF-8 / {proj.get('dxf_version') or 'AC1021'} 阅读。")
    lines.append(f"当前图幅：{region_label(region)}")
    lines.append(f"当前范围（世界坐标包围盒）：{region['bbox']}")

    sheets = index.get("sheets") or []
    # Identify the host sheet
    host = None
    sid = region.get("sheet_id")
    for s in sheets:
        if s.get("id") == sid:
            host = s
            break
        if sid and sid in (s.get("id") or "") or sid in (s.get("title") or ""):
            host = s
            break
    if host:
        lines.append(
            f"图幅记录：id={host.get('id')} 图名={host.get('title')} 楼层={host.get('floor')} 比例={host.get('scale')} 图框={_bbox(host.get('frame'))}"
        )
        if host.get("notes"):
            lines.append(f"图幅备注：{host['notes']}")
        for sub in host.get("subviews") or []:
            name = sub.get("name")
            lines.append(
                f"分图：{name} 包围盒={_bbox(sub.get('frame'))} 比例={sub.get('scale')}"
            )

    tokens = _tokens(user_text)
    # Pull matching fixtures from the full equipment list (including nested ones).
    hits = indexing.search_equipment(index, user_text, region=region, limit=40)
    if hits:
        lines.append(f"本图幅匹配设备（{len(hits)} 条，含底图块内嵌套符号；坐标为世界坐标）：")
        for e in hits:
            lines.append("  " + indexing.format_equipment_line(e))

    rooms = index.get("rooms") or []
    # Prefer rooms on this sheet/floor, then ones matching the query
    scored: list[tuple[int, dict[str, Any]]] = []
    for room in rooms:
        label = " ".join(
            str(room.get(k) or "") for k in ("name", "sheet", "floor", "notes")
        )
        score = 0
        sheet_label = room.get("sheet") or ""
        if sid and sid in sheet_label:
            score += 5
        if region.get("title") and region["title"] in sheet_label:
            score += 4
        if region.get("kind") == "room" and room.get("name") == region.get("title"):
            score += 6
        if region.get("floor") and room.get("floor") == region.get("floor"):
            score += 8
        for t in tokens:
            if t and t in label:
                score += 3
        if score:
            scored.append((score, room))
    scored.sort(key=lambda x: -x[0])
    picked_rooms = [r for _, r in scored[:18]]
    if not picked_rooms:
        # fall back to rooms whose sheet id matches
        picked_rooms = [r for r in rooms if sid and sid in (r.get("sheet") or "")][:12]
    focus = [r for r in picked_rooms if _is_focus_room(r, region)]
    if focus:
        rest = [r for r in picked_rooms if r not in focus]
        picked_rooms = focus + rest
    if picked_rooms:
        lines.append("房间：")
        for room in picked_rooms:
            bit = (
                f"- {room.get('name')}  坐标={room.get('world_xy')}  "
                f"楼层={room.get('floor')}  所在={room.get('sheet')}"
            )
            if room.get("notes"):
                bit += f"  备注={room.get('notes')}"
            lines.append(bit)
            sheet_label = room.get("sheet") or ""
            on_current = bool(
                (region.get("title") and region["title"] in sheet_label)
                or (region.get("sheet_id") and region["sheet_id"] in sheet_label)
            )
            full = _is_focus_room(room, region) or on_current or (
                bool(tokens) and any(t and t in (room.get("name") or "") for t in tokens)
            )
            lines.extend(_equipment_lines(room, full=full))

    blocks = index.get("blocks") or []
    wanted_names = set(tokens)
    for staple in ("地漏", "马桶", "洗手盆", "清扫口", "地漏DL", "dilou", "消火栓", "蹲便", "坐便", "座便", "便器", "小便", "台盆", "淋浴", "洁具"):
        wanted_names.add(staple)
    picked_blocks = []
    for b in blocks:
        name = b.get("name") or ""
        display = b.get("display_name") or ""
        aliases = " ".join(b.get("aliases") or [])
        blob = f"{name} {display} {aliases} {b.get('category') or ''}"
        if any(t and t in blob for t in wanted_names if len(t) >= 2):
            picked_blocks.append(b)
        if len(picked_blocks) >= 16:
            break
    if picked_blocks:
        lines.append("相关图块（本地包围盒，插入时用世界坐标）：")
        for b in picked_blocks:
            bb = b.get("local_bbox") or {}
            disp = b.get("display_name")
            extra = f"  符号={disp}" if disp and disp != b.get("name") else ""
            lines.append(
                f"- {b.get('name')}  插入次数={b.get('insert_count')}{extra}  "
                f"典型图层={b.get('typical_layer')}  本地包围盒={bb}"
            )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…（摘录已截断）"
    return text


def _load_chat_unlocked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"messages": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"messages": []}
    if isinstance(data, list):
        return {"messages": data}
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data
    return {"messages": []}


def _write_chat_unlocked(path: Path, data: dict[str, Any]) -> None:
    atomicio.write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_chat(proj: dict[str, Any]) -> dict[str, Any]:
    path = proj.get("chat_path")
    if not path:
        return {"messages": []}
    with chat_lock(proj):
        return _load_chat_unlocked(Path(path))


def append_chat(proj: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    path = Path(proj["chat_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with chat_lock(proj):
        data = _load_chat_unlocked(path)
        data.setdefault("messages", [])
        existing = {msg.get("id") for msg in data["messages"] if msg.get("id")}
        added = False
        for msg in messages:
            mid = msg.get("id")
            if mid and mid in existing:
                continue
            data["messages"].append(msg)
            if mid:
                existing.add(mid)
            added = True
        if added:
            data["updated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
            _write_chat_unlocked(path, data)
        return data


def update_chat_message(proj: dict[str, Any], message_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Patch fields on a saved chat message (e.g. overlay once the preview is ready)."""
    path = proj.get("chat_path")
    if not path or not message_id:
        return None
    dest = Path(path)
    with chat_lock(proj):
        data = _load_chat_unlocked(dest)
        found = None
        for msg in data.get("messages") or []:
            if msg.get("id") == message_id:
                msg.update(fields)
                found = msg
                break
        if found is None:
            return None
        data["updated_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        _write_chat_unlocked(dest, data)
        return found


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def region_inventory(proj: dict[str, Any], region: dict[str, Any]) -> dict[str, Any]:
    index = load_index(proj)
    rooms = []
    equipment = 0
    names = []
    for room in index.get("rooms") or []:
        if not _is_focus_room(room, region) and region.get("kind") == "room":
            if (room.get("name") or "") != (region.get("title") or ""):
                continue
        if region.get("kind") == "sheet" and region.get("sheet_id") not in (room.get("sheet") or ""):
            continue
        if region.get("kind") == "room" and (room.get("name") or "") != (region.get("title") or ""):
            # still allow same-name rooms on this sheet
            if region.get("sheet_id") and region.get("sheet_id") not in (room.get("sheet") or ""):
                continue
            if (room.get("name") or "") != (region.get("title") or ""):
                continue
        eq = room.get("equipment_nearby") or []
        equipment += len(eq)
        rooms.append({"name": room.get("name"), "equipment": len(eq), "xy": room.get("world_xy")})
        names.append(room.get("name"))
    summary = ""
    if region.get("kind") == "room":
        n = equipment
        summary = f"当前分区：{region.get('title')}。邻近设备 {n} 项。"
    elif rooms:
        summary = f"当前图幅：{region.get('title')}。识别到 {len(rooms)} 个房间。"
    else:
        summary = f"当前图幅：{region.get('title')}。"
    return {"summary": summary, "rooms": rooms, "equipment_count": equipment}
