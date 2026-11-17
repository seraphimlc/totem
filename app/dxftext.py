"""DXF 文字的唯一清洗与改写入口。

索引、场景、改文字、替换块都走这里，保证「图面上看到的字」和「说出来的字」
逐字一致——标点是内容的一部分，不做全角/半角互换，只规整空白。

规则：
- MTEXT：优先 ezdxf 的 MTEXT 纯文本解析（fast_plain_mtext），不可用时退到本模块
  的保守解析（`\\{` `\\}` 还原本字，只写格式码的花括号不显示，不丢字）。
- TEXT / ATTRIB / ATTDEF：解码 %%c %%d %%p %%% 等控制码，解完逐字复核。
- 只把换行、制表、连续空白规整成单个空格；全角 ，。；：、（）“”‘’《》 和
  半角 ,.;:()"' 等标点原样保留。

改写侧（改文字用）：
- TEXT：``encode_text_control`` 每个 % 写三遍（%% 会被当成控制码前缀），回读正好。
- MTEXT：``rewrite_mtext_body`` 保留整段格式外壳（``{\\fSimSun|b0|i0;旧}``、
  前缀 ``\\A1;``）只换可见正文；多段混合格式时整段换成同字体的一段，用户文字里的
  ``\\ { }`` 转义，换行写 ``\\P``，% 照 TEXT 的规则写。写完一律用
  ``check_visible`` 复核，不是逐字相等就当失败。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Optional

# ---------------------------------------------------------------- 清洗


def normalize_whitespace(text: str) -> str:
    """换行/制表/连续空白折成一个空格，首尾去空白；不碰其它字符。"""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _lines(text: Any) -> list[str]:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line.rstrip() for line in text.split("\n")]


def edit_visible(text: Any) -> str:
    """改文字统一的「可见文字」口径（两边都按它比）。

    空白规整、标点一字不动：每行去掉行尾空白；行首/行尾的空行去掉；
    其余换行原样保留（MTEXT 的 ``\\P`` 结构按段落数比：``line1\\P\\Pline3``
    就是「line1 / 空行 / line3」）。TEXT 不能存换行，比较前再把换行折成单个空格。
    """
    lines = _lines(text)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def edit_visible_flat(text: Any) -> str:
    """同一口径的「单行形态」：换行与连续空白折成一个空格（TEXT 的显示方式）。"""
    return re.sub(r"\s+", " ", edit_visible(text))


def edit_visible_matches(raw: Any, requested: Any, *, kind: str = "mtx") -> str:
    """写回后的原始串是否与要求一致；一致返回可见字，不一致抛 ValueError。

    只比 :func:`edit_visible` 这一种空白口径（标点必须逐字相等）：
    - ``kind="txt"``（TEXT/ATTRIB）：先做控制码校验，再把换行折成空格（TEXT 写不了换行）；
    - ``kind="mtext"``（MTEXT）：**按原始串逐行**去行尾空白后与要求比，与
      ``check_visible`` 的行处理一致，但不会把整段折成一行，免得把「a 换行 b」误当成「a b」。
    """
    if kind == "txt":
        got = check_visible(raw, kind="txt")
        want = edit_visible_flat(requested)
        if edit_visible_flat(got) != want:
            raise ValueError(f"写入后可见文字对不上（写入「{got}」，应为「{want}」）")
        return got
    text = str(raw or "")
    # 原始串里的 \P 就是段落分隔：换成换行后按同一口径比
    rows_only = re.sub(r"\\[Pp]", "\n", text)
    got = edit_visible(rows_only) if "\\" not in rows_only.replace("\n", "") else edit_visible(check_visible(text))
    want = edit_visible(requested)
    if got != want:
        raise ValueError(f"写入后可见文字对不上（写入「{got}」，应为「{want}」）")
    return got


# TEXT 控制码的可见值（与 ezdxf.tools.text.plain_text 一致，大小写都认）：
# %%c → Ø，%%d → °，%%p → ±，%%% → %；%%u / %%o / %%k 是下划线/上划线开关，本身不带字。
# %%<数字>（如 %%50）在 AutoCAD 里是字形码，ezdxf 原样保留 —— 这里也原样保留，绝不
# 换成「?」，否则用户的字就丢了。
_PERCENT_CODES = {
    "c": "Ø",
    "d": "°",
    "p": "±",
    "%": "%",
    "u": "",
    "o": "",
    "k": "",
}
_PERCENT_NUMBER = re.compile(r"%%(\d{1,3})")


def percent_visible(text: str) -> str:
    """TEXT 控制码 → 可见字，与 ``ezdxf.tools.text.plain_text`` 逐字一致。

    ``%%c``/``%%C`` → Ø，``%%d`` → °，``%%p`` → ±，``%%%`` → %；
    ``%%u``/``%%o``/``%%k``（含大写）是开关，去掉；``%%数字``（最多三位）原样保留
    ——绝不换成「?」，那是用户的字；单独 % 原样。换行先去掉（单行文字），
    结尾孤立的 ``%%`` 与 ezdxf 的处理一致（去掉，不进可见字）。
    """
    text = str(text or "").replace("\r\n", "").replace("\r", "").replace("\n", "")
    if "%%" not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("%%", i):
            m = _PERCENT_NUMBER.match(text, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
            code = text[i + 2 : i + 3]
            if code and code.lower() in _PERCENT_CODES:
                out.append(_PERCENT_CODES[code.lower()])
                i += 3
                continue
            if i + 2 >= n:
                # 结尾孤立的 %%，ezdxf 当作控制码前缀吃掉：可见字里没有它
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def decode_percent_codes(text: str) -> str:
    """TEXT 控制码解码：见 percent_visible（与 ezdxf plain_text 一致）。"""
    return percent_visible(text)


def _mtx_unescape(code: str) -> str:
    if not code:
        return ""
    head = code[0]
    if head in "{}\\":
        return head
    if head.lower() == "p":
        return " "
    if head in "~":
        return " "
    if head.lower() == "s":
        return ""
    if head in "nN":
        return " "
    if head.lower() == "t":
        return "\t"
    if head == "S":  # 堆叠 \S1^2; → 1/2
        body = code[1:]
        for sep in ("^", "#"):
            if sep in body:
                a, b = body.split(sep, 1)
                return f" {a}/{b} "
        return " " + body
    return ""  # \A \C \f \H \W \Q \T \p… 格式码无可见字


_MTX_FMT = r"(?:\\(?:[fFCHWQT|][^;{}]*;|px[^;{}]*;|p[^;{}]*;|A\d(?:;)?|[lLoOkK]))"

# ---------------------------------------------------------------- 保守解析


def _mtx_group_inner(text: str, start: int) -> Optional[str]:
    """text[start]=='{' 时返回配对组内的原文（不含花括号）；不配对返回 None。"""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1
    return None


def _mtx_format_only(body: str) -> bool:
    """组内是否只有格式码（没有可见字）。`{\fA;…}` 是格式组，`{甲、乙}` 不是。"""
    return bool(body.strip()) and re.fullmatch(_MTX_FMT + r"\s*", body) is not None


def _mtx_strip_outer(text: str, depth_guard: int = 8) -> str:
    """剥掉「开头一串格式码整段包住」的最外层 {...}；可见字一个字都不动。"""
    s = str(text or "")
    for _ in range(depth_guard):
        s2 = s.strip()
        if not s2.startswith("{"):
            return s
        inner = _mtx_group_inner(s2, 0)
        if inner is None or len(inner) != len(s2) - 2:
            return s
        m = re.match(r"(?:" + _MTX_FMT + r")+\s*(?=\S|$)", inner)
        if not m or m.end() >= len(inner):
            return s
        s = inner
    return s


def _fallback_clean_mtext(raw: str) -> str:
    """保守的 MTEXT 解析：保留 `\\{` `\\}` `\\\\`，`\\P` 换行，%% 码按 TEXT 规则解码。

    只写格式码的花括号不显示；直接写字的组（`{甲、乙}`）花括号是可见字符，原样保留。
    未知转义序列去掉转义符保留其后字符——宁可多留一个字，也不把用户的字吃掉。
    """
    out: list[str] = []
    i = 0
    text = str(raw or "")
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in "{}\\":
                out.append(nxt)
                i += 2
                continue
            j = i + 1
            while j < n and text[j] not in ";{}":
                j += 1
            if j < n and text[j] == ";":
                out.append(_mtx_unescape(text[i + 1 : j] + ";"))
                i = j + 1
            else:
                out.append(_mtx_unescape(text[i + 1 : j]))
                i = j
            continue
        if ch == "{":
            inner = _mtx_group_inner(text, i)
            if inner is not None and _mtx_format_only(inner):
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    plain = "".join(out)
    plain = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), plain)
    return decode_percent_codes(plain)


_PERCENT_STRICT = re.compile(r"%%(c|C|d|D|p|P|%|\?|[0-9])")


def _visible_plain(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def check_visible(raw: Any, *, kind: str = "mtx") -> str:
    """严格版清洗：返回可见文字（MTEXT 保留换行为 ``\\n``），对不上则抛 ValueError。

    写入路径用它当闸门：改完立刻按它复核，相等才落笔，否则算失败。
    - ``mtx``：以 ezdxf 的 MTEXT 纯文本解析为准，不可用时退到本模块的保守解析。
    - ``txt``：%% 解码后其余字符必须与原文逐字相等（控制码只认 %%x / %%数字）。
    """
    text = str(raw or "")
    if kind == "txt":
        got = decode_percent_codes(text)
        if _visible_plain(got) != _visible_plain(percent_visible(text)):
            raise ValueError("TEXT 控制码无法可靠解码")
        return got
    visible = _mtx_visible(text)
    if visible is None:
        raise ValueError("MTEXT 内容无法可靠解析")
    return visible


def _mtx_primary(text: str) -> Optional[str]:
    """ezdxf 的 MTEXT 纯文本（会去格式组、`\\P` 换成行首换行）。不可用时返回 None。"""
    try:
        from ezdxf.tools.text import fast_plain_mtext
    except Exception:
        return None
    try:
        return str(fast_plain_mtext(text) or "").replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        return None


def _normalize_lines(text: str) -> str:
    """MTEXT 可见字的行处理：去行尾空白与首尾空行，行内空行保留（\\P\\P 是两个换行）。"""
    return edit_visible(text)


def _mtx_visible(text: str) -> str:
    """MTEXT 原文 → 可见字（保留换行）。

    以 ezdxf 的 MTEXT 解析为准；它不可用时退到本模块的保守解析（只写格式码的花括号
    不显示，`\\{` `\\}` 还原本字，直接写字的组保留花括号，不丢字）。
    """
    primary = _mtx_primary(text)
    if primary is not None and primary.strip():
        # \\U+XXXX 是 R2000 及以前写非 ASCII 字符的方式，新版解析器不转
        primary = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), primary)
        return _normalize_lines(primary)
    fallback = _normalize_lines(_fallback_clean_mtext(_mtx_strip_outer(text)))
    if fallback.strip():
        return fallback
    return _normalize_lines(primary or "")


def clean_mtext(raw: Any) -> str:
    """MTEXT 原始内容 → 可见文字（保留标点，只规整空白，换行折成空格）。"""
    text = str(raw or "")
    if not text.strip():
        return ""
    return normalize_whitespace(_mtx_visible(text))


def clean_text_value(raw: Any) -> str:
    """TEXT / ATTRIB / ATTDEF 的 dxf.text → 可见文字。"""
    text = str(raw or "")
    if not text.strip():
        return ""
    return normalize_whitespace(decode_percent_codes(text))


def entity_raw(ent) -> str:
    """实体的原始文字内容（TEXT 是控制码原样，MTEXT 是格式码原样）。"""
    kind = ent.dxftype()
    if kind == "MTEXT":
        try:
            return str(ent.text or "")
        except Exception:
            return ""
    try:
        return str(ent.dxf.text or "")
    except Exception:
        return ""


def clean_entity_text(ent) -> str:
    """任何 TEXT / MTEXT / ATTRIB / ATTDEF → 可见文字。"""
    try:
        kind = ent.dxftype()
    except Exception:
        return ""
    raw = entity_raw(ent)
    if kind == "MTEXT":
        return clean_mtext(raw)
    return clean_text_value(raw)


# ---------------------------------------------------------------- 改写


def _would_decode(plain: str) -> bool:
    """TEXT 或 MTEXT 解码后与原文不同，就说明里面有会成控制码的 %。"""
    try:
        from ezdxf.tools.text import fast_plain_mtext, plain_text

        return plain_text(plain) != plain or fast_plain_mtext(plain) != plain
    except Exception:
        # ezdxf 不可用时按最保守的规则：有 % 就补
        return "%" in plain


def escape_percent_codes(plain: str) -> str:
    """% 的最小可逆转义（TEXT 与 MTEXT 通用）。

    不含控制码的就不动（50% 原样）；只要原文会被解码成别的东西，就把每个 % 写成
    %%%（两个 % 会被吃掉一个，所以补三个）：%%c → %%%%%%c 回读是 %%c，
    %%50 → %%%%%%50 回读是 %%50。已在 "%%cdpuok5a " 上对任意 1–5 字字符串穷举：
    两个 ezdxf 解码器都能逐字还原（scripts/test_text_replace.py 里有同样的穷举）。
    """
    text = str(plain or "")
    if "%" not in text:
        return text
    if not _would_decode(text):
        return text
    return text.replace("%", "%%%")


def encode_text_control(plain: str) -> str:
    """可见文字 → TEXT 的 dxf.text：最小化 %% 转义，换行变空格；回读逐字相等。"""
    text = str(plain or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ")
    return escape_percent_codes(text)


_NEWLINE_SPLIT = re.compile(r"\r\n|\r|\n")


def _escape_for_mtext(text: str) -> str:
    """用户文字 → MTEXT 可见正文：反斜杠、花括号转义；% 按同一套最小化转义。

    % 的转义只看 % 本身（不看反斜杠/花括号），所以先按原文判断再整体替换。
    """
    raw = str(text or "")
    escaped = "".join("\\" + ch if ch in "\\{}" else ch for ch in raw)
    return escaped.replace("%", "%%%") if escape_percent_codes(raw) != raw else escaped


def _scan_group(s: str, start: int) -> tuple[int, int, bool]:
    """从 s[start]=='{' 找配对 '}'，返回 (内容起, 内容止, 闭合处是否即字符串末尾)。"""
    depth = 0
    i = start
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return start + 1, i, i == n - 1
        i += 1
    return start + 1, n, False


def _split_prefix_groups(s: str) -> tuple[list[str], str, bool]:
    """开头的连续 `{…}` 组；返回 (组列表, 剩余部分, 所有组都在 s 末尾闭合)。"""
    s = str(s or "")
    groups: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        if s[i] == "{":
            cstart, cend, _tail = _scan_group(s, i)
            groups.append(s[i : cend + 1])
            i = cend + 1
            continue
        break
    return groups, s[i:], i >= n


def _leading_codes(s: str) -> tuple[list[str], int]:
    """开头的裸格式码（`\\A1;` `\\H1.2x;` …），返回 (码列表, 可见正文起点)。"""
    s = str(s or "")
    codes: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        if s[i] == "\\" and i + 1 < n and s[i + 1] not in "{}\\~":
            j = i + 1
            while j < n and s[j] not in ";{}":
                j += 1
            if j < n and s[j] == ";":
                codes.append(s[i : j + 1])
                i = j + 1
                continue
        break
    return codes, i


def _container(s: str) -> Optional[tuple[str, str, str]]:
    """单个 `{…}` 组 → (组头含 '{' 与格式码, 内容, 组尾含 '}')；否则 None。"""
    s = str(s or "").strip()
    if not s or s[0] != "{":
        return None
    cstart, cend, tail = _scan_group(s, 0)
    if not tail:
        return None
    head_codes, vis = _leading_codes(s[1:cend])
    return "{" + "".join(head_codes), s[cstart + vis : cend], s[cend:]


def rewrite_mtext_body(raw: str, new_visible: str) -> str:
    """保留原来的格式外壳/前缀，只替换可见正文。

    依次尝试，取第一个「重新清洗后可见字与要求逐字相等」的方案：
      1. 整段外壳：``{\\fSimSun|b0|i0;旧}`` → ``{\\fSimSun|b0|i0;新}``；
         ``\\A1;{\\f…;旧}`` → ``\\A1;{\\f…;新}``。
      2. 前缀格式码：``\\A1;旧`` → ``\\A1;新``；``\\H1.2x;{\\fA;甲}`` →
         ``\\H1.2x;{\\fA;新}``。
      3. 多段混合格式（不同颜色/字体逐段排版，如 ``\\A1;{\\C1;红}\\C2;蓝；绿``）：
        段落码 ``\\A1;`` 等原文里已有的开头前缀保留，正文拍平成**一段**，多段颜色不再
        逐段保留。可见文字逐字不变。
    兜底时**只保留原文真实出现过的段首格式码**，绝不新增 ``\\f`` 等格式码：原文没有
    字体码就只写正文（``abc`` → ``新``），MTEXT 条目自带的文字样式因此不会被覆盖。
    用户文字里的 ``\\ { }`` 转义，换行写 ``\\P``，%+数字/%% 写成 %%%。
    改完一律用 :func:`edit_visible_matches` 复核（空白规整、标点逐字）。
    """
    raw = str(raw or "")
    new_visible = str(new_visible or "").replace("\r\n", "\n").replace("\r", "\n")
    escaped = "\\P".join(_escape_for_mtext(seg) for seg in _NEWLINE_SPLIT.split(new_visible))
    want = _visible_plain(new_visible)

    def _accept(cand: Optional[str]) -> Optional[str]:
        if not cand:
            return None
        if _visible_plain(clean_mtext(cand)) == want:
            return cand
        return None

    if not raw.strip():
        return escaped

    candidates: list[str] = []
    codes, body_start = _leading_codes(raw)
    body = raw[body_start:]
    body = body.rstrip() if not body.lstrip().startswith("{") else body
    groups, rest, tail_all = _split_prefix_groups(body)
    outer = _container(body) if tail_all and not rest.strip() else None
    if outer:
        head, content, close = outer
        inner_groups, inner_rest, _inner_tail = _split_prefix_groups(content)
        if inner_groups and not inner_rest.strip():
            inner = _container(inner_groups[0])
            if inner:
                candidates.append("".join(codes) + head + inner[0] + escaped + inner[2] + close)
            else:
                candidates.append("".join(codes) + head + escaped + close)
        else:
            inner_codes, vis = _leading_codes(content)
            if inner_codes and content[vis:].strip():
                # 格式码写在外层、组内：{\A1;旧}
                candidates.append("".join(codes) + head + "".join(inner_codes) + escaped + close)
            elif not inner_groups:
                candidates.append("".join(codes) + head + escaped + close)
            else:
                first = _container(inner_groups[0])
                if first:
                    candidates.append("".join(codes) + head + first[0] + escaped + first[2] + close)
    if codes and body.strip():
        candidates.append("".join(codes) + escaped)
        if groups and groups[0].startswith("{"):
            candidates.append("".join(codes) + groups[0].rsplit("}", 1)[0][:-1] + escaped + "}")
    for cand in candidates:
        hit = _accept(cand)
        if hit:
            return hit
    # 兜底：只保留原文里真实出现过的段首格式码（\A1; \pxqc; 等），正文拍平成一段。
    # 绝不凭空加 \f 之类：MTEXT 条目自带的文字样式（dxf.style）会因为内嵌字体码而失效。
    prefix = codes
    if groups and groups[0].startswith("{"):
        head_codes, _vis = _leading_codes(groups[0][1:])
        prefix = prefix + head_codes
    hit = _accept("".join(prefix) + escaped)
    if hit:
        return hit
    return escaped


# ---------------------------------------------------------------- 查询


def primary_anchor(ent) -> Optional[list[float]]:
    """文字的锚点：优先对齐点（非 0,0），否则插入点。返回世界坐标。"""
    kind = ent.dxftype()
    try:
        if kind in {"TEXT", "ATTRIB", "ATTDEF"}:
            halign = int(getattr(ent.dxf, "halign", 0) or 0)
            valign = int(getattr(ent.dxf, "valign", 0) or 0)
            if (halign or valign) and ent.dxf.hasattr("align_point"):
                p = ent.dxf.align_point
                if p is not None:
                    return [float(p.x), float(p.y)]
        p = ent.dxf.insert
        return [float(p.x), float(p.y)]
    except Exception:
        return None


def text_points(ent) -> list[tuple[float, float]]:
    """参与测距的点：锚点、插入点、对齐点。"""
    pts: list[tuple[float, float]] = []
    for attr in ("insert", "align_point"):
        try:
            if ent.dxf.hasattr(attr):
                p = getattr(ent.dxf, attr)
                if p is not None:
                    pts.append((float(p.x), float(p.y)))
        except Exception:
            continue
    anchor = primary_anchor(ent)
    if anchor and (anchor[0], anchor[1]) not in pts:
        pts.append((anchor[0], anchor[1]))
    return pts


def text_extent(ent) -> Optional[tuple[float, float, float, float]]:
    """粗估文字占据的范围（按字高 × 字数），用于「点在文字上」的判断。"""
    anchor = primary_anchor(ent)
    if not anchor:
        return None
    try:
        if ent.dxftype() == "MTEXT":
            h = float(ent.dxf.char_height or 2.5)
        else:
            h = float(ent.dxf.height or 2.5)
    except Exception:
        h = 2.5
    try:
        s = ent.dxftype() == "MTEXT" and float(ent.dxf.width or 0) or 0.0
    except Exception:
        s = 0.0
    visible = clean_entity_text(ent)
    w = max(h, h * 0.6 * max(len(visible), 1))
    if s > 0:
        w = min(w, s)
    try:
        rot = math.radians(float(getattr(ent.dxf, "rotation", 0) or 0))
    except Exception:
        rot = 0.0
    # 以锚点为一端，按旋转把「向右延伸的文字框」包成大包围盒
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    xs, ys = [], []
    for lx, ly in ((0.0, 0.0), (w, 0.0), (w, h), (0.0, h)):
        xs.append(anchor[0] + lx * cos_r - ly * sin_r)
        ys.append(anchor[1] + lx * sin_r + ly * cos_r)
    return (min(xs), min(ys), max(xs), max(ys))


def _fold_width(text: str) -> str:
    """全角 ASCII 区（！～，含 （）；：、引号）→ 半角；NFKC 之外再兜一层。"""
    out = []
    for ch in str(text or ""):
        code = ord(ch)
        if ch == "\u3000":
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return unicodedata.normalize("NFKC", "".join(out))


def width_fold(text: str) -> str:
    """忽略全/半角与空白的比对形式（只在精确匹配失败后作第二遍）。"""
    return re.sub(r"\s+", "", _fold_width(text))
