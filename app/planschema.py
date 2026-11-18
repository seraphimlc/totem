"""改图方案的结构校验：归一化 + blockers / warnings / needs_clarification。

为什么要它：方案 JSON 一直只由提示词里的那段字面量约定，键名拼错、坐标写成布尔/
NaN、`rot` 写成字符串这类问题，要么被 `apply_mutations()` 静默判成「格式不对」，
要么要等到坐标校验才出现，甚至可能先删了旧图块再发现新块插不上。这里在**动图之前**
把结构查清楚：

- 未知键（方案的、条目的）丢弃并给 warning（advisory，可以照常预览/应用）；
- 坐标、类型不合法（缺坐标、布尔、NaN/Infinity、`rot` 传字符串、`scale` 非正……）
  是 blocker（阻断，不写图）；
- 同一角色的别名互相矛盾（删除的 xy 和 from_xy 指向两处、移动的 to 和 xy 指向两处……）
  那一条整个不执行、给 warning；全篇只剩冲突、没有能确定位置的改动时是 blocker，
  并标 needs_clarification（问用户，不猜先出现的那个）；
- 「模型没把话说清楚」（plan 模式却没有改动、删除只给块名没给位置、非对象方案）是
  needs_clarification（需要澄清，直接问用户，不再浪费一次模型修复往返）；
- 目标落在保护图层（图框/图签/标题/TITLE/BORDER/SEAL）是 blocker。

issue 的形状和 `placement._issue()` 一致：``{"code","severity","message","op","index","block","xy"}``，
severity 只有 ``error``（blocker）/ ``warn``（warning）。

必须看图纸才能知道的事由 ``app.apply`` 在动图前再判一次（预览与应用走同一套）：要删 / 移 /
换的 INSERT 显示在哪层（``effective_layer``），它里面的内容——嵌套插入、块内文字、属性
——有没有落在保护图层（``analyze_insert``）。layer 0 的继承只走 INSERT 链。

本模块只做结构与图层判断，不读 DXF、不写盘，也不做几何校验（那是 placement 的事）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# 保护图层：方案里指明落进这些图层、或要动的实体（含嵌套/继承后的实际图层）落在这些
# 图层上，一律 blocker。匹配是「图层名里包含」且不区分大小写，所以「图框-0」「A1图框」
# 「title-block」也算。名单按真实图纸的命名习惯可再补（见 README「保护图层」）。
PROTECTED_LAYERS: tuple[str, ...] = ("图框", "图签", "标题", "TITLE", "BORDER", "SEAL")

# 对照 cad-ai-agent 的 plan_validator.py：不在图腾里借它的动作/实体类型兼容与审批规则。
VALID_MODES = ("answer", "critique", "plan", "reject")
EDIT_KEYS = ("inserts", "deletes", "replaces", "moves", "pipes", "texts", "text_edits")
# 方案顶层认识的键；其余（模型自创的调试键等）丢弃并 warning。
PLAN_KEYS = frozenset(
    {"mode", "reply", "room", "sheet", "analysis", "do_not_touch", "issues", *EDIT_KEYS}
)
# issues 是系统回填的校验结果，模型不应自己编；出现了也只丢弃（不拦，旧方案里可能带 []）。
ITEM_KEYS = {
    "inserts": frozenset({"block", "xy", "to", "rot", "scale", "layer"}),
    "deletes": frozenset({"block", "from_block", "xy", "from_xy", "from"}),
    "replaces": frozenset({"from_block", "from_xy", "from", "block", "xy", "to", "rot", "scale", "layer", "auto_attribs"}),
    "moves": frozenset({"block", "from", "from_xy", "to", "xy", "rot", "scale", "layer"}),
    "pipes": frozenset({"layer", "from", "to", "const_width"}),
    "texts": frozenset({"text", "xy", "layer", "style", "height"}),
    "text_edits": frozenset({"from_text", "xy", "text"}),
}

OP_LABEL = {
    "insert": "新增",
    "delete": "删除",
    "replace": "替换",
    "move": "移动",
    "pipe": "管线",
    "text": "文字",
    "text_edit": "改文字",
}
_ARRAY_OPS = {
    "inserts": "insert",
    "deletes": "delete",
    "replaces": "replace",
    "moves": "move",
    "pipes": "pipe",
    "texts": "text",
    "text_edits": "text_edit",
}

# DXF 坐标是有限实数；布尔在 Python 里是 int 的子类，必须单独排除。
MAX_ABS_COORD = 1e9

# 块引用最多往下看多少层；再深 / 成环就判不清，按保护处理（宁可不改）。
MAX_BLOCK_DEPTH = 32


# ---------------------------------------------------------------- 图层


def protected_pattern(name: Optional[str]) -> Optional[str]:
    """图层名命中的保护关键词（原样大小写），未命中返回 None。"""
    text = str(name or "").strip().upper()
    if not text:
        return None
    for key in PROTECTED_LAYERS:
        if key.upper() in text:
            return key
    return None


def is_protected_layer(name: Optional[str]) -> bool:
    return protected_pattern(name) is not None


def _block_defs(doc) -> list:
    """文档里的块定义（不含模型空间 / 图纸空间这类布局）。"""
    out = []
    try:
        names = [record.dxf.name for record in doc.block_records]
    except Exception:
        return out
    layouts = {"*Model_Space", "*Paper_Space"}
    for name in getattr(doc, "layout_names", lambda: [])():
        try:
            layouts.add(doc.layout(name).block_record.dxf.name)
        except Exception:
            continue
    for name in names:
        if name in layouts:
            continue
        try:
            layout = doc.blocks.get(name)
        except Exception:
            layout = None
        if layout is not None:
            out.append(layout)
    return out


def _slots(doc) -> list:
    """能直接放实体的地方：模型空间、各布局、各块定义。"""
    slots = []
    try:
        slots.append(doc.modelspace())
    except Exception:
        pass
    for name in getattr(doc, "layout_names", lambda: [])():
        try:
            slots.append(doc.layout(name))
        except Exception:
            continue
    return slots + _block_defs(doc)


def _attrib_hosts(insert):
    """这个 INSERT 上的属性，以及本图里嵌套插入上的属性（用于 layer 0 继承）。

    ATTRIB 的宿主就是它所属的 INSERT，不再往上找别的宿主；嵌套插入在块定义里没有
    ``doc`` 或 ``get_layout()``，要沿块定义自己找回宿主链，所以由文档级的插入来提供。
    """
    out = list(getattr(insert, "attribs", None) or [])
    doc = getattr(insert, "doc", None)
    try:
        name = str(insert.dxf.name)
    except Exception:
        return out
    if doc is None or not name:
        return out
    for e in _iter_block_entities(doc, name):
        if getattr(e, "dxftype", lambda: "")() == "INSERT":
            out.extend(getattr(e, "attribs", None) or [])
    return out


# 实体的「直接宿主 INSERT」（属性 → 所属 INSERT，块内容 → 引用它的 INSERT）。块定义里的
# 实体没有 doc，只能从 document 级的 INSERT 反查，所以按实体记录缓存：不缓存的话，每往上一层
# 都要把图里所有块定义扫一遍。
_PARENT_CACHE_KEY = "_totem_parent_insert"


def _attrib_parent(ent):
    """属性所属的那个 INSERT（``insert.attribs`` 里的那个）。"""
    doc = getattr(ent, "doc", None)
    if doc is None:
        return None
    for parent in doc.modelspace().query("INSERT"):
        try:
            if any(a is ent for a in getattr(parent, "attribs", None) or []):
                return parent
        except Exception:
            continue
    return None


def _dedicated_parent(ent):
    """块记录上的 INSERT 专用宿主（```insert.dxf.get("block_record_handle")```）：命中本图某个块记录的就是它。

    这种 INSERT 带着专属 TAG 组码，``get_layout()`` 会先看它，而不是看 owner；块定义里的图元
    也要沿它对上号。对不上就不猜，交给下面的声明式查找。
    """
    doc = getattr(ent, "doc", None)
    if doc is None:
        return None
    try:
        dedicated = str(ent.dxf.get("block_record_handle") or "")
        owner = str(ent.dxf.get("owner") or "")
    except Exception:
        return None
    if not dedicated:
        return None
    try:
        record = doc.entitydb.get(dedicated)
    except Exception:
        record = None
    if record is None or not hasattr(record, "block_layout"):
        return None
    for slot in _slots(doc):
        try:
            if not _is_layout(slot, doc) and slot.block_record is record:
                return slot
        except Exception:
            continue
    if owner != dedicated:
        return None
    try:
        parent = doc.entitydb.get(owner)
    except Exception:
        parent = None
    if parent is not None and getattr(parent, "dxftype", lambda: "")() == "INSERT":
        return parent
    return None


def _parent_insert(ent):
    """把这个实体放进图里的那个 INSERT（属性 → 所属 INSERT，块内容 → 引用它的 INSERT）。

    INSERT 的图层、它自己的 ATTRIB 都以这个 INSERT 为宿主（属性画的层是 0 就跟着 INSERT
    走）。在块定义里、布局里直接放的实体没有宿主；找宿主**永不越过**本实体所在的那一层。
    """
    doc = getattr(ent, "doc", None)
    if doc is None:
        return None
    try:
        kind = ent.dxftype()
    except Exception:
        return None
    if kind == "ATTRIB":
        # 属性先按所属 INSERT 找：有的写法里属性的 owner 指到别处，get_layout() 会抛错。
        return _attrib_parent(ent)
    try:
        layout = ent.get_layout()
    except Exception:
        layout = None
    if layout is None:
        # 没有 layout 的实体只会是块定义里的内容，块的「图层」不参与继承。
        return None
    if any(layout is slot for slot in _slots(doc) if _is_layout(slot, doc)):
        return ent if kind == "INSERT" else None
    record = getattr(layout, "block_record", None)
    if record is not None:
        dedicated = _dedicated_parent(ent)
        if dedicated is not None:
            return dedicated
    try:
        name = layout.name
    except Exception:
        return None
    # 块定义图元：找「把这个块插在哪」的 INSERT，它的块名就是被定义的块（``name``）。
    for slot in _slots(doc):
        try:
            for child in slot.query(f'INSERT[name=="{name}"]'):
                return child
        except Exception:
            continue
    return None


def _cached_parent_insert(ent):
    """``_parent_insert`` 的内存缓存（按实体对象，不跨文档、不写盘）。"""
    cached = getattr(ent, _PARENT_CACHE_KEY, None)
    if cached is not None:
        return cached[0]
    parent = _parent_insert(ent)
    # 只缓存确定的结果；判不清的下次再查（可能是暂时读不到）。
    if parent is not None:
        try:
            setattr(ent, _PARENT_CACHE_KEY, (parent,))
        except Exception:
            pass
    return parent


def _is_layout(slot, doc) -> bool:
    try:
        return slot.name in doc.layout_names()
    except Exception:
        return False


def _resolved_chain(ent) -> list[str]:
    """从实体往上一层层的 (自身图层, 所在 INSERT)。layer 0 继承只看 INSERT，不看 BLOCK 头。"""
    chain: list[str] = []
    node = ent
    for _ in range(MAX_BLOCK_DEPTH + 2):
        try:
            chain.append(str(getattr(node.dxf, "layer", "") or "").strip())
        except Exception:
            break
        if chain[-1] not in ("", "0"):
            break
        parent = _cached_parent_insert(node)
        if parent is None or parent is node:
            break
        node = parent
    return chain


def effective_layer(ent) -> str:
    """实体显示在哪一层：自身图层不是 "0" 就用它，是 "0" 就按**引用它的 INSERT** 的图层。

    这就是 CAD 里 layer 0 的继承：块里画在 0 层的线，插进图框层的 INSERT 后看起来就在
    图框上；属性（ATTRIB）同样跟随所属 INSERT。继承沿 INSERT 链一层层往上走，块记录 /
    BLOCK 头上的图层不参与继承（那个组码不属于渲染）。全链都是 0 就返回 "0"。
    """
    for layer in _resolved_chain(ent):
        if layer not in ("", "0"):
            return layer
    return "0"


def _mirrored(insert) -> bool:
    """镜像 / 负比例插入：渲染器按炸开后的插入继承，镜像下会退化成原图层。"""
    try:
        return float(insert.dxf.xscale) < 0 or float(insert.dxf.yscale) < 0
    except Exception:
        return False


def _is_xref(doc, name: str) -> bool:
    """外部参照：内容不在本图里，判不清也不该动。"""
    try:
        return bool(doc.blocks.get(name).block_record.is_xref)
    except Exception:
        return False


def _iter_block_entities(doc, name: str) -> list:
    """块定义里的实体（只读）；块缺失或读不了返回 []。"""
    try:
        block = doc.blocks.get(name)
        return list(block) if block is not None else []
    except Exception:
        return []


def _child_protected(doc, name: str, host_layer: str, depth: int, seen: set[str], out: list[str], state: dict[str, bool]) -> None:
    """块 name 插在 host_layer 上时，里面的内容有没有落在保护图层（沿 INSERT 链表出图层）。

    layer 0 只继承**直接引用它的 INSERT** 的图层（``host_layer`` 是调用方按 INSERT 链算出来
    的），块定义的「图层」不参与继承。
    """
    if depth > MAX_BLOCK_DEPTH:
        state["depth"] = True
        return
    if name in seen:
        state["cycle"] = True
        return
    try:
        entities = list(doc.blocks.get(name)) if doc.blocks.get(name) is not None else None
    except Exception:
        entities = None
    if entities is None or _is_xref(doc, name):
        state["error"] = True
        return
    seen.add(name)
    for e in entities:
        try:
            kind = e.dxftype()
            layer = str(e.dxf.layer or "").strip()
        except Exception:
            state["error"] = True
            continue
        shown = host_layer if layer in ("", "0") else layer
        if is_protected_layer(shown):
            out.append(shown)
        if kind == "INSERT":
            try:
                child = str(e.dxf.name)
            except Exception:
                state["error"] = True
                continue
            _child_protected(doc, child, _nested_shown_layer(e, shown), depth + 1, seen, out, state)


def _nested_shown_layer(child, shown: str) -> str:
    """嵌套插入显示在哪层：自己的图层不是 0 就用它；是 0 就继承直接宿主（外层块插在哪）。

    宿主就是外层块里这个子 INSERT 所在的这一层 ``shown``，它已经是终点，不用再往上找。
    """
    try:
        own = str(child.dxf.layer or "").strip()
    except Exception:
        return shown
    return shown if own in ("", "0") else own


def _attrib_protected(insert, out: list[str]) -> bool:
    """INSERT 自带的 ATTRIB（含嵌套插入的属性，按 0 层继承）有没有落在保护图层。

    读不了的属性返回 True：判不清就按保护处理。
    """
    try:
        attribs = _attrib_hosts(insert)
    except Exception:
        return True
    for att in attribs:
        try:
            raw = str(att.dxf.layer or "").strip()
        except Exception:
            return True
        shown = effective_layer(att) if raw in ("", "0") else raw
        if is_protected_layer(shown):
            out.append(shown)
    return False


def analyze_insert(insert) -> dict[str, Any]:
    """这个 INSERT 删掉 / 移走 / 换掉会不会碰到保护内容；判不清时按保护处理。

    返回 ``{"protected", "protected_where", "flags", "cycle", "depth", "xref", "mirrored"}``：

    - ``protected_layer``：块里（含嵌套块，按 0 层继承后）有落在保护图层上的内容；
    - ``xref``：外部参照；``mirrored``：镜像插入（layer 0 的继承在渲染时会退化）；
    - ``cycle`` / ``depth``：块引用成环 / 超过 ``MAX_BLOCK_DEPTH``；
    - ``unresolvable``：块缺失、内容读不了、MINSERT，或块里有 0 层的内容找不到可继承的宿主。

    0 层是**合法的显示图层**：模型空间里自己画在 0 层的 INSERT 照样能删 / 移 / 换，按 0 层
    写入。块内容和 ATTRIB 的 0 层沿 INSERT 链继承，块定义上的「图层」不参与。INSERT 自己的
    显示图层也不在这里判（调用方用 ``effective_layer(insert)`` 判，然后照常按保护图层拦）。
    """
    flags: list[str] = []
    info: dict[str, Any] = {"protected": False, "protected_where": "", "flags": flags}
    try:
        name = str(insert.dxf.name)
        doc = insert.doc
    except Exception:
        name, doc = "", None
    where = ""
    state = {"cycle": False, "depth": False, "error": False}
    found: list[str] = []
    if doc is not None and name:
        state["error"] = _attrib_protected(insert, found)
        # 块里 0 层的内容继承这个 INSERT 自己的图层；这个 INSERT 就在布局里，它的图层
        # （包括 0）就是终点，所以这里不存在「找不到宿主」。
        _child_protected(doc, name, effective_layer(insert), 0, set(), found, state)
    else:
        state["error"] = True
    if found:
        where = found[0]
    for key in ("cycle", "depth"):
        if state[key]:
            info[key] = True
            flags.append(key)
    if state["error"]:
        flags.append("unresolvable")
    if doc is not None and name and _is_xref(doc, name):
        info["xref"] = True
        flags.append("xref")
    if _mirrored(insert):
        info["mirrored"] = True
        flags.append("mirrored")
    if where or flags:
        info["protected"] = True
        info["protected_where"] = where
        flags.insert(0, "protected_layer")
    return info


# ---------------------------------------------------------------- 结果


def _issue(code: str, severity: str, message: str, op: str = "", index: Any = None, block: str = "", xy=None) -> dict[str, Any]:
    """与 placement._issue() 同构的 dict。"""
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "op": op,
        "index": index,
        "block": block or "",
        "xy": list(xy) if isinstance(xy, (list, tuple)) and len(xy) >= 2 else None,
    }


BlockedItem = tuple[dict[str, Any], int]


@dataclass
class PlanResult:
    """一次归一化的结果。

    blockers：阻断项，每项 ``(issue, 原数组下标)``；``issue["index"]`` 是归一化后数组里的
    下标（坏条目会被剔除，两者可能不同）。warnings：仅提示。valid=没有 blocker；
    needs_clarification=应当直接问用户而不是写图或再问模型。
    """

    plan: dict[str, Any]
    blockers: list[BlockedItem] = field(default_factory=list)
    warnings: list[BlockedItem] = field(default_factory=list)
    needs_clarification: bool = False
    status: str = "valid"

    @property
    def valid(self) -> bool:
        return not self.blockers

    def issue_dicts(self, *, kind: str = "all") -> list[dict[str, Any]]:
        """``blockers`` / ``warnings`` / 两者（默认），都是同构 dict。"""
        if kind == "blockers":
            return [i for i, _ in self.blockers]
        if kind == "warnings":
            return [i for i, _ in self.warnings]
        return [i for i, _ in self.blockers] + [i for i, _ in self.warnings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valid": self.valid,
            "needs_clarification": self.needs_clarification,
            "blockers": self.issue_dicts(kind="blockers"),
            "warnings": self.issue_dicts(kind="warnings"),
        }


# ---------------------------------------------------------------- 值校验


class _Bad(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_bool(value) -> bool:
    return isinstance(value, bool)


def _number(value, *, what: str) -> float:
    if _is_bool(value):
        raise _Bad("bad_type", f"{what}不能是布尔值")
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num):
            raise _Bad("bad_coordinate", f"{what}不是有限数字（NaN/Infinity）")
        return num
    if isinstance(value, str):
        raise _Bad("bad_type", f"{what}必须是数字，现在是字符串「{value}」")
    raise _Bad("bad_type", f"{what}必须是数字")


def _xy(value, *, what: str = "坐标", required: bool = True) -> Optional[list[float]]:
    if value is None:
        if required:
            raise _Bad("missing_xy", f"缺少{what}（xy），需要 [x, y] 两个数字")
        return None
    if isinstance(value, dict):
        raise _Bad("bad_coordinate", f"{what}必须是 [x, y] 两个数字，不能是对象")
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _Bad("bad_coordinate", f"{what}必须是 [x, y] 两个数字")
    if len(value) != 2:
        raise _Bad("bad_coordinate", f"{what}必须是正好 [x, y] 两个数字，现在是 {len(value)} 个")
    x = _number(value[0], what=f"{what} x")
    y = _number(value[1], what=f"{what} y")
    if abs(x) > MAX_ABS_COORD or abs(y) > MAX_ABS_COORD:
        raise _Bad("bad_coordinate", f"{what}超出可处理范围（超过 1e9）")
    return [x, y]


def _block_name(value) -> str:
    if value is None:
        raise _Bad("missing_block", "缺少块名（block）")
    if not isinstance(value, str):
        raise _Bad("bad_type", "块名（block）必须是字符串")
    name = value.strip()
    if not name:
        raise _Bad("missing_block", "块名（block）不能为空")
    return name


def _rot(value) -> Optional[float]:
    if value is None or value == "":
        return None
    num = _number(value, what="旋转角 rot（单位：度）")
    if abs(num) > 1e6:
        raise _Bad("bad_value", "旋转角 rot 超出合理范围")
    num = math.fmod(num, 360.0)
    if num < 0:
        num += 360.0
    return float(round(num, 6))


def _scale(value) -> Optional[Any]:
    if value is None:
        return None
    if _is_bool(value):
        raise _Bad("bad_type", "缩放 scale 不能是布尔值")
    if isinstance(value, (int, float)):
        num = _number(value, what="缩放 scale")
        if num <= 0:
            raise _Bad("bad_value", "缩放 scale 必须大于 0")
        return float(num)
    if isinstance(value, (list, tuple)):
        if len(value) not in (2, 3):
            raise _Bad("bad_value", "缩放 scale 必须是 [sx, sy] 两个数字")
        out = [_number(v, what="缩放 scale") for v in value]
        if any(v <= 0 for v in out):
            raise _Bad("bad_value", "缩放 scale 必须大于 0")
        return out
    raise _Bad("bad_type", "缩放 scale 必须是数字或 [sx, sy]")


def _text(value, *, what: str, required: bool = True) -> Optional[str]:
    if value is None:
        if required:
            raise _Bad("missing_text", f"缺少{what}")
        return None
    if not isinstance(value, str):
        raise _Bad("bad_type", f"{what}必须是字符串")
    if required and not value.strip():
        raise _Bad("missing_text", f"缺少{what}")
    return value


def _layer(value) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Bad("bad_type", "图层 layer 必须是字符串")
    return value.strip()


# ---------------------------------------------------------------- 逐类条目


def _scrub(item: dict[str, Any], allowed: frozenset, op: str, idx: int, known_bad=None) -> list[dict[str, Any]]:
    """丢掉条目里不认识的键，返回提示（advisory）；不拦下整条。"""
    notes = []
    for extra in item:
        if extra in allowed or (known_bad and extra in known_bad):
            continue
        notes.append(
            _issue("unknown_key", "warn", f"{_label(op, idx)}里有多余字段「{extra}」，已忽略。", op, idx)
        )
    return notes


def _common_attrs(out: dict[str, Any], item: dict[str, Any], op: str) -> None:
    rot = _rot(item.get("rot"))
    if rot is not None:
        out["rot"] = rot
    scale = _scale(item.get("scale"))
    if scale is not None:
        out["scale"] = scale
    layer = _layer(item.get("layer"))
    if layer is not None:
        out["layer"] = layer


def _same(a, b) -> bool:
    return a == b


def _pick_block(item: dict[str, Any], keys: tuple[str, ...], *, what: str = "块名") -> str:
    """同名别名（block/from_block）里取一个；都给了且不一致 → 冲突，不猜。"""
    given = [(k, _block_name(item.get(k))) for k in keys if item.get(k) is not None]
    if not given:
        raise _Bad("missing_block", f"缺少{what}（{keys[0]}）")
    first = given[0][1]
    for key, value in given[1:]:
        if value.casefold() != first.casefold():
            raise _Bad("alias_conflict", f"「{first}」和「{value}」是两个不同的块名，{what}只该有一处，没法确定按哪个")
    return first


def _pick_xy(item: dict[str, Any], keys: tuple[str, ...], *, what: str = "坐标", required: bool = True):
    """同角色的坐标别名（如删除的 xy / from_xy / from）里取一个。

    每个给了的别名都要校验：写坏了就是 blocker，不会因为主键能取到值就被跳过；都合法
    但不一致 → 冲突（needs_clarification），绝不按先出现的那个静默执行。
    """
    given = []
    for key in keys:
        if item.get(key) is None:
            continue
        try:
            given.append((key, _xy(item.get(key), what=what, required=True)))
        except _Bad as bad:
            raise _Bad(bad.code, f"{key}：{bad.message}")
    if not given:
        if required:
            raise _Bad("missing_xy", f"缺少{what}（{keys[0]}），需要 [x, y] 两个数字")
        return None
    first_key, first = given[0]
    for key, value in given[1:]:
        if not _same(value, first):
            raise _Bad(
                "alias_conflict",
                f"同一个位置给了两种说法（{first_key}={_fmt_xy(first)}，{key}={_fmt_xy(value)}），不确定要动哪一处",
            )
    return first


def _fmt_xy(xy) -> str:
    return f"[{round(float(xy[0]), 3)}, {round(float(xy[1]), 3)}]"


def _clean_insert(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out: dict[str, Any] = {"block": _pick_block(item, ("block",))}
    notes = _scrub(item, ITEM_KEYS["inserts"], "insert", idx)
    # xy 与 to 都是「插在哪」：冲突就拦，不再「以 xy 为准」。
    out["xy"] = _pick_xy(item, ("xy", "to"))
    _common_attrs(out, item, "insert")
    return out, notes


def _clean_delete(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _scrub(item, ITEM_KEYS["deletes"], "delete", idx)
    out: dict[str, Any] = {"block": _pick_block(item, ("block", "from_block"))}
    # 只有块名、没给位置：模型没说清删哪一个（图上常有多处同名块）→ 问用户。
    try:
        out["xy"] = _pick_xy(item, ("xy", "from_xy", "from"))
    except _Bad as bad:
        if bad.code == "missing_xy":
            raise _Bad("ambiguous_target", f"删除「{out['block']}」没有给坐标，无法确定删哪一处")
        raise
    return out, notes


def _clean_replace(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _scrub(item, ITEM_KEYS["replaces"], "replace", idx)
    old_name = _pick_block(item, ("from_block",), what="旧块名")
    new_name = _pick_block(item, ("block",), what="新块名")
    # 同名替换照常放过（写图时按删除 + 新增处理），不在这里另定规则。
    out: dict[str, Any] = {"from_block": old_name, "block": new_name}
    # 起点（from_xy/from）与终点（xy/to）是两种角色，各取一个；各角色内部冲突照拦。
    out["from_xy"] = _pick_xy(item, ("from_xy", "from"), what="旧块坐标", required=False) or _pick_xy(
        item, ("xy", "to"), what="新块坐标"
    )
    out["xy"] = _pick_xy(item, ("xy", "to"), what="新块坐标") or out["from_xy"]
    _common_attrs(out, item, "replace")
    if "auto_attribs" in item:
        if not isinstance(item.get("auto_attribs"), bool):
            raise _Bad("bad_type", "auto_attribs 必须是 true/false")
        out["auto_attribs"] = item["auto_attribs"]
    return out, notes


def _clean_move(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _scrub(item, ITEM_KEYS["moves"], "move", idx)
    out: dict[str, Any] = {"block": _pick_block(item, ("block",))}
    out["from"] = _pick_xy(item, ("from", "from_xy"), what="起点")
    out["to"] = _pick_xy(item, ("to", "xy"), what="终点")
    _common_attrs(out, item, "move")
    return out, notes


def _clean_pipe(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _scrub(item, ITEM_KEYS["pipes"], "pipe", idx)
    out: dict[str, Any] = {}
    layer = _layer(item.get("layer"))
    if layer is not None:
        out["layer"] = layer
    out["from"] = _xy(item.get("from"), what="管线起点")
    out["to"] = _xy(item.get("to"), what="管线终点")
    width = item.get("const_width")
    if width is not None:
        num = _number(width, what="管线宽度 const_width")
        if num < 0:
            raise _Bad("bad_value", "管线宽度 const_width 不能为负数")
        out["const_width"] = num
    return out, notes


def _clean_text(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _scrub(item, ITEM_KEYS["texts"], "text", idx)
    out: dict[str, Any] = {"text": _text(item.get("text"), what="文字内容 text")}
    out["xy"] = _xy(item.get("xy"), what="文字坐标")
    for key in ("layer", "style"):
        val = item.get(key)
        if val is not None and not isinstance(val, str):
            raise _Bad("bad_type", f"{key} 必须是字符串")
        if isinstance(val, str):
            out[key] = val.strip()
    height = item.get("height")
    if height is not None and height != "":
        num = _number(height, what="文字高度 height")
        if num < 0:
            raise _Bad("bad_value", "文字高度 height 不能为负数")
        # 0 与提示词里的默认写法一致：写入端按默认高度处理，这里原样保留。
        out["height"] = num
    return out, notes


def _clean_text_edit(item: dict[str, Any], idx: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    notes = _scrub(item, ITEM_KEYS["text_edits"], "text_edit", idx)
    # 原文字与新文字逐字保留（含首尾空白与全/半角标点），只在匹配时规整空白。
    out = {
        "from_text": _text(item.get("from_text"), what="原文字（from_text）"),
        "text": _text(item.get("text"), what="新文字（text）"),
        "xy": _xy(item.get("xy"), what="改文字坐标"),
    }
    return out, notes


_CLEANERS = {
    "inserts": _clean_insert,
    "deletes": _clean_delete,
    "replaces": _clean_replace,
    "moves": _clean_move,
    "pipes": _clean_pipe,
    "texts": _clean_text,
    "text_edits": _clean_text_edit,
}


def _label(op: str, idx: int, block: str = "") -> str:
    name = OP_LABEL.get(op, op)
    if op in {"insert", "delete", "replace", "move"}:
        name += f"「{block}」" if block else ""
    return f"{name}第 {idx + 1} 项"


# ---------------------------------------------------------------- 主入口


# 「模型没把话说清楚」：直接问用户，不再回喂模型修。同名别名的冲突也是这种——两个位置
# 都像真的，只有用户知道要动哪一处。
CLARIFY_CODES = frozenset({"ambiguous_target", "bad_plan", "no_edits", "alias_conflict"})


def normalize_plan(plan: Any) -> PlanResult:
    """把模型给的方案归一化成 {plan, blockers, warnings, needs_clarification}。

    不修改入参；坏条目从归一化后的数组里剔除（同时记 blocker），所以即使调用方不看
    blockers，也写不进坏的条目。合法旧方案原样通过：值不变、不产生 warning。
    """
    blockers: list[BlockedItem] = []
    warnings: list[BlockedItem] = []

    if not isinstance(plan, dict):
        issue = _issue("bad_plan", "error", "方案的格式不对：整体必须是一个 JSON 对象，请重新给出改图方案。", "plan", None)
        return PlanResult(plan={}, blockers=[(issue, -1)], needs_clarification=True, status="needs_clarification")

    out: dict[str, Any] = {}
    for key, value in plan.items():
        if key in PLAN_KEYS:
            out[key] = value
            continue
        issue = _issue("unknown_key", "warn", f"方案里有多余字段「{key}」，已忽略。")
        warnings.append((issue, -1))

    mode = out.get("mode")
    if mode is not None:
        mode_s = str(mode).strip().lower() if isinstance(mode, str) else ""
        if mode_s not in VALID_MODES:
            blockers.append((_issue("bad_mode", "error", f"mode 只能是 answer / critique / plan / reject，现在是「{mode}」。"), -1))
        else:
            out["mode"] = mode_s
    reply = out.get("reply")
    if reply is not None and not isinstance(reply, str):
        blockers.append((_issue("bad_type", "error", "reply 必须是字符串。"), -1))
        out.pop("reply", None)

    # 逐类归一化；坏条目剔除并记 blocker，条目里的未知键只提示。
    # 回填的 ``plan["issues"]``（系统给的校验结果）可能带着 warning，再归一化一次时它们
    # 不再出现（条目上的未知键已经剔除、冲突条目已经剔除）。那不代表这一版变「干净」了：
    # 带过 warning 的方案第二次归一化仍算 valid_with_warnings，状态不降级。
    fed_back_issues = bool(out.get("issues"))
    for key in EDIT_KEYS:
        op = _ARRAY_OPS[key]
        if key not in out:
            continue
        raw = out[key]
        if raw is None:
            out[key] = []
            continue
        if not isinstance(raw, list):
            blockers.append((_issue("bad_structure", "error", f"{key} 必须是一个数组。", op, None), -1))
            out[key] = []
            continue
        clean: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                blockers.append((_issue("bad_structure", "error", f"{OP_LABEL[op]}第 {idx + 1} 项必须是对象，现在不是。", op, len(clean)), idx))
                continue
            block = item.get("block") if isinstance(item.get("block"), str) else item.get("from_block")
            try:
                cleaned, notes = _CLEANERS[key](item, idx)
            except _Bad as bad:
                if bad.code == "alias_conflict":
                    # 同角色的两种说法冲突（xy 和 from_xy 指向两处……）：条目整个剔除，但
                    # 不当作写图错误——模型只是没说清，问用户比让它再猜一轮靠谱。
                    warnings.append(
                        (
                            _issue("alias_conflict", "warn", f"{_label(op, idx, block if isinstance(block, str) else '')}：{bad.message}。请确认要动的那一处后再试。", op, len(clean), block if isinstance(block, str) else ""),
                            idx,
                        )
                    )
                    continue
                xy = item.get("xy") or item.get("from_xy") or item.get("to") or item.get("from")
                blockers.append(
                    (
                        _issue(
                            bad.code,
                            "error",
                            f"{_label(op, idx, block if isinstance(block, str) else '')}：{bad.message}",
                            op,
                            len(clean),
                            block if isinstance(block, str) else "",
                            xy if isinstance(xy, (list, tuple)) and len(xy) >= 2 and all(_is_plain_number(v) for v in list(xy)[:2]) else None,
                        ),
                        idx,
                    )
                )
                continue
            for note in notes:
                note["index"] = len(clean)
                warnings.append((note, idx))
            layer = cleaned.get("layer")
            if layer and is_protected_layer(layer):
                blockers.append(
                    (
                        _issue(
                            "protected_layer",
                            "error",
                            f"{_label(op, idx, cleaned.get('block') or cleaned.get('from_block') or '')}指定了保护图层「{layer}」（图框/图签/标题等），不会写入。",
                            op,
                            len(clean),
                            cleaned.get("block") or cleaned.get("from_block") or "",
                            cleaned.get("xy") or cleaned.get("to") or cleaned.get("from"),
                        ),
                        idx,
                    )
                )
                continue
            clean.append(cleaned)
        out[key] = clean

    has_edits = any(out.get(k) for k in EDIT_KEYS)
    blockers.sort(key=lambda b: (b[0]["op"] or "", -1 if b[0]["index"] is None else b[0]["index"]))
    if out.get("mode") == "plan" and not has_edits and any(i.get("code") == "alias_conflict" for i, _ in warnings) and not blockers:
        names = "、".join(sorted({str(i.get("op")) for i, _ in warnings if i.get("code") == "alias_conflict"}))
        blockers.append((_issue("alias_conflict", "error", f"这次方案里没有一处能确定位置的改动（{names}的位置说法互相矛盾），请确认要动哪一处。", "plan", None), -1))
    elif out.get("mode") == "plan" and not has_edits and not blockers:
        blockers.append((_issue("no_edits", "error", "这次方案是 plan 模式，但没有任何可执行的改动，请重新说明要改什么。", "plan", None), -1))

    # 状态在 blockers / warnings 之后再定；``seen_issues`` 只用来兜住「回填 issues 是唯一
    # 的 warning 来源」这种情况（此时状态保持 valid_with_warnings，不因为它是回填的而丢）。
    needs_clarification = any(i.get("code") in {"ambiguous_target", "bad_plan", "no_edits", "alias_conflict"} for i, _ in blockers)
    if blockers:
        status = "needs_clarification" if needs_clarification else "blocked"
    elif warnings or fed_back_issues:
        status = "valid_with_warnings"
    else:
        status = "valid"
    return PlanResult(plan=out, blockers=blockers, warnings=warnings, needs_clarification=needs_clarification, status=status)


def _is_plain_number(value) -> bool:
    return isinstance(value, (int, float)) and not _is_bool(value) and math.isfinite(float(value))


def clarify_message(result: PlanResult) -> str:
    """给用户看的中文说明：说清哪几项被拦下、为什么，不要技术术语。"""
    blocked = result.issue_dicts(kind="blockers")
    if not blocked:
        return ""
    lines = ["这次没有动图：方案里有几处没法安全执行，需要你确认或换个说法。"]
    for issue in blocked[:4]:
        msg = str(issue.get("message") or "").strip()
        if msg and msg not in lines:
            lines.append(f"- {msg}")
    if len(blocked) > 4:
        lines.append(f"- 另有 {len(blocked) - 4} 处同类问题。")
    return "\n".join(lines)


def warning_message(result: PlanResult) -> str:
    """给用户看的中文提醒：仅提示、不影响预览/应用。"""
    warns = result.issue_dicts(kind="warnings")
    if not warns:
        return ""
    bits = [str(w.get("message") or "").strip() for w in warns[:3] if w.get("message")]
    if len(warns) > 3:
        bits.append(f"另有 {len(warns) - 3} 处")
    return "提示：" + "；".join(bits) + "（只作提醒，不影响这次改图）。"


__all__ = [
    "PROTECTED_LAYERS",
    "VALID_MODES",
    "EDIT_KEYS",
    "PlanResult",
    "clarify_message",
    "effective_layer",
    "is_protected_layer",
    "normalize_plan",
    "protected_pattern",
    "warning_message",
]
