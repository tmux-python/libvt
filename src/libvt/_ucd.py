"""Generated two-stage Unicode 17.0.0 cell-width and UAX #29 table.

Regenerate with ``uv run python scripts/generate_ucd.py``.
"""

from __future__ import annotations

import array
import base64
import enum
import sys
import zlib


class WidthRole(enum.IntEnum):
    """Column occupancy role of a terminal grid cell."""

    NARROW = 0
    WIDE = 1
    SPACER_TAIL = 2
    SPACER_HEAD = 3


class ClusterState(enum.IntEnum):
    """Scalar UAX #29 grapheme cluster continuation state."""

    NONE = 0
    BASE = 1
    RI_ODD = 2
    EXT_PICT = 3
    EXT_PICT_ZWJ = 4
    INCB_CONS = 5
    INCB_LINKER = 6


PROP_NONE = 0
PROP_CONTROL = 1
PROP_EXTEND = 2
PROP_ZWJ = 3
PROP_REGIONAL_INDICATOR = 4
PROP_SPACING_MARK = 5
PROP_EXTENDED_PICTOGRAPHIC = 6
PROP_INCB_CONSONANT = 7
PROP_INCB_LINKER = 8
PROP_VS15 = 9
PROP_VS16 = 10
PROP_EMOJI_MODIFIER = 11

EXT_PICT_BIT = 0x40
BIDI_SPOOF_BIT = 0x80

UNICODE_VERSION = "17.0.0"

_PACKED_B85 = (
    "c-rk8hoa;pa9E?Fp5;{RtLu5{*?aH3_ZsZIpMJ!BxP+1rLJ}aPY{JajeVc?Zg@G9=oB{@j;SSD&4"
    "9<rO;6lJ~5nK$HzzMh%E`!VA3ikXYPle!0xC*WY0@uK`a2>yMJ>0;)Z-kp*4>!Xt?Dv6vZiQ2D8="
    "QvQ;SRVH&cI!($Ge%-_pr~sa39<c4~#_RL3qeO<c&o>5A$D-z@zN@F?bxFsHJ$4e?J9J!!z(KJO|"
    "Ii3-BVm1TVuY@G86pufrSgCcFi2!#mDY-woi@4c}|rc^^KIzdj7s^hfY<1l~SjIeiMBS)P3E^!^v"
    "@^Cjd!Yj?lWet*qm{f7Pj7QTb;+5aEl$6DH-;Ai*+eudxQclZPTgumc#_{Xq@gj>b4Ne3G*f0Qs`"
    "!h{JECQO(xVZww76DCZUFk!;g;=k};(n7+72@`G*vmQX>Lgq|AFH+|yL0rHSL(+I#5hO+U8zf>V{"
    ")RO`R`Tdv1kM|Yrc`2v6cb!a4mOtcF*r}vjzXoG1@20!r7y4isD;^M3$MWyC+n+3q><I$1u(&g5K"
    "`vC;6R9CL#5)TXMyrLd(nwLD*wv_Me#}c2o;$y`ehD8WP=*?i}z}^(N+G6zWP)2h4PEfL9?lnSF@"
    "B1y9=XVmml+7boot+t@aY2f1N(I>R-wvblqpemdATlc>BG|X0N_0hKgb)f_<ik;$xJ*Q>`gf=p(;"
    "IpEnAH-Kz$ol3()cAiuBv_lEujeJ+1Vzm{c#KB-#)39SEnT-CoW{Rhb3$pq4Vm8n?zgOHqvuz*Nt"
    "=1a&hS7Z&?rVDSe-8saj-<cJUy8?A-_#V}KHQc;cXJV45O4zz|c;tXp+uxE@1p!2~jmjDurNr|Qk"
    "Fj2FYz<R<*o_QZ@5>x#2W*H?z5X5#=a6VYC$BhF9EuO2%>s{=XHvOSyFZgPa0O%OO9@eJ)v8A<3V"
    "FljBc*|&gKnFp=1TvCLu#kbXj@+g-QEH0i<zbGmUZtC+W&6p{usLCiW?T*U}3;2-0*h%^@8hg{EK"
    "iomIzCRBV|)U)>3;Tjz?$NiLUvMa|P7y<gFLR_#%IIlKB)Ae*k{^IhUM6<L1O%P+B8@9HDI9Ar7T"
    "XtL#Iij#uKUw=yTF7g!!cdS(e!*0nltLummjq3yoySqD%vhx$GJ%95|VRXw3*rk0FYYAUa({-~R8"
    ">LiTJh&GdZ6-<W?jR$mYOI%6S%6lDvtJf&M5~LBC`Fr(CUyso-ayZGD<ZhJ^qVGNs_ENh=cVj8AF"
    "x=@h<&%D7c1E^`(fEm6pI8L6ORzNvx1Xb$Tygil@%^)XMH=pkMfm9dSiwrjY&<cbOG`Zw5B7_qT*"
    "%bt4f?D$X!yMx6DMwC?>Jf@DTo?F_!VQmvm_w_vs%w6tr>gJQ)^AgrWV_Z>Cs|^4vrZQE+Lv74%x"
    "<G=I-gj=8sOFF|E$@9)U44T4<1xt$hJdD;E>Y#Vz0XlZZ36I?EXm)s|m+4x}E31r5}7l)_4Tyk;&"
    "%yXTJ{Ma*Me;hh6ahDz)u0Irv#clVc6o!=sQsW(AckQ~n9k~V4m1~L5BzqP8s7;F&xFh;c`L0E0M"
    "Ql+`<HbYZZOTh;32jOe`#r_|;|EVHD>c&xV5UbHC<tj+=%{K(8y)PpVAtGd~g~Y99hsPsL)VkzNc"
    "~x!0W54j@7yYEl$NEz0X(^Hvy)_&U-l`5nQ>#4g(gu^u7D8^^Ks}pb{bM+_Yg~T^n@|1shnTC@SJ"
    "G(_{*kQ3{Oj8@$FTjDbxNeNj9HDNisVo=l^Y|_kvq9XP?<&w#w<ZC<i{8VbikAibTxD9EJAbs2oD"
    "<$SiGrdE@|z19ye0?hZSNvtgBl+jm0^Rps6=1Lwx0Gt^9^9h&=Z7zZUh(I(xga<s^#-eSrdUTi<K"
    "u(3gKB$q4I%6KtU(FkdkCp6MayDWqdfJ%cWJAff6ym`d%>WTC<xxj&d;!^t9hc%H{bgdaA6okj|*"
    "=b()wKWcC4P{84*1NsjMMpMt$9aVDFo0MN$q@or&t~}J5xR{SQ26jE$L!IG~PXH!=wMXU+^E?o53"
    "hl(E2dW<=<IZ<4oNNVZobo|Epq9RTA6fESuKo84X+7j8r%@pHAW5A#?SEg^clPUS0fQxoq>5P}&j"
    ")8<s5*B_*>|E6rQT;Rg)}zfsB{h{Dm|5@ipnG3DpJbdX>C@%T4=U!{JO*C4RL|Z{QO+1n+vw_+ai"
    "xg{1a%4IgRZPW@!S)jp59@W&qK8<p+q`oVmsP&3I+=Gax;)3-j*3UhMei(Sj-e|BJnGm;"
)


def _unpack_tables() -> tuple[array.array[int], bytes]:
    raw = zlib.decompress(base64.b85decode(_PACKED_B85))
    s1_len = int.from_bytes(raw[:4], "little")
    s1 = array.array("H")
    s1.frombytes(raw[4 : 4 + s1_len])
    if sys.byteorder != "little":
        s1.byteswap()
    return s1, raw[4 + s1_len :]


_STAGE1, _STAGE2 = _unpack_tables()


def ucd_lookup(cp: int) -> int:
    """Return packed 8-bit width and UAX #29 property entry for codepoint *cp*."""
    if 0 <= cp <= 0x10FFFF:
        return _STAGE2[(_STAGE1[cp >> 8] << 8) | (cp & 0xFF)]
    return PROP_CONTROL << 2


def char_width(ch: str) -> int:
    """Return default terminal column width (``0``, ``1``, or ``2``) for *ch*."""
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:
        return 1
    return ucd_lookup(cp) & 0b11


def step_cluster(
    prev_state: ClusterState,
    cp: int,
    *,
    mode_2027: bool,
) -> tuple[bool, ClusterState, int]:
    """Evaluate UAX #29 continuation between *prev_state* and codepoint *cp*.

    Returns
    -------
    tuple[bool, ClusterState, int]
        ``(extends_previous, next_state, width_override)`` where
        ``width_override`` is ``-1`` (keep current cluster width), ``1``
        (narrow via ``VS15``), or ``2`` (widen via ``VS16``, emoji modifier,
        ZWJ pictographic, or Regional Indicator pair).
    """
    entry = ucd_lookup(cp)
    prop = (entry >> 2) & 0x0F

    if prev_state != ClusterState.NONE:
        if prop == PROP_EXTEND:
            return True, prev_state, -1
        if prop == PROP_VS16:
            return True, prev_state, (2 if mode_2027 else -1)
        if prop == PROP_VS15:
            return True, prev_state, (1 if mode_2027 else -1)
        if mode_2027:
            if prop == PROP_SPACING_MARK:
                next_st = (
                    prev_state
                    if prev_state in (ClusterState.INCB_CONS, ClusterState.EXT_PICT)
                    else ClusterState.BASE
                )
                return True, next_st, -1
            if prop == PROP_ZWJ:
                next_st = (
                    ClusterState.EXT_PICT_ZWJ
                    if prev_state == ClusterState.EXT_PICT
                    else ClusterState.BASE
                )
                return True, next_st, -1
            if prop == PROP_EMOJI_MODIFIER and prev_state == ClusterState.EXT_PICT:
                return True, ClusterState.EXT_PICT, 2
            if prev_state == ClusterState.EXT_PICT_ZWJ and (entry & EXT_PICT_BIT):
                return True, ClusterState.EXT_PICT, 2
            if prop == PROP_REGIONAL_INDICATOR and prev_state == ClusterState.RI_ODD:
                return True, ClusterState.BASE, 2
            if prop == PROP_INCB_LINKER and prev_state == ClusterState.INCB_CONS:
                return True, ClusterState.INCB_LINKER, -1
            if prop == PROP_INCB_CONSONANT and prev_state == ClusterState.INCB_LINKER:
                return True, ClusterState.INCB_CONS, -1

    # Start of a new base cell (or dropped standalone zero-width character).
    if prop == PROP_REGIONAL_INDICATOR and mode_2027:
        return False, ClusterState.RI_ODD, -1
    if entry & EXT_PICT_BIT:
        return False, ClusterState.EXT_PICT, -1
    if prop == PROP_INCB_CONSONANT:
        return False, ClusterState.INCB_CONS, -1
    if (entry & 0b11) == 0:
        return False, ClusterState.NONE, -1
    return False, ClusterState.BASE, -1


def display_width(text: str, *, mode_2027: bool = True) -> int:
    """Return rendered terminal column width of *text*."""
    if text.isascii():
        return sum(1 for ch in text if " " <= ch <= "~")
    total = 0
    cur_w = 0
    state = ClusterState.NONE
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E:
            total += cur_w
            cur_w = 1
            state = ClusterState.BASE
            continue
        extends, state, w_override = step_cluster(state, cp, mode_2027=mode_2027)
        if extends:
            if w_override != -1:
                cur_w = w_override
        else:
            total += cur_w
            cur_w = ucd_lookup(cp) & 0b11
    return total + cur_w


def sanitize_terminal_text(text: str) -> str:
    """Escape C0, C1, ``DEL``, and BiDi override codepoints for safe display."""
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E:
            out.append(ch)
        elif cp < 0x20 or 0x7F <= cp <= 0x9F or (ucd_lookup(cp) & BIDI_SPOOF_BIT):
            out.append(f"\\x{cp:02x}" if cp <= 0xFF else f"\\u{cp:04x}")
        else:
            out.append(ch)
    return "".join(out)


__all__ = [
    "BIDI_SPOOF_BIT",
    "UNICODE_VERSION",
    "ClusterState",
    "WidthRole",
    "char_width",
    "display_width",
    "sanitize_terminal_text",
    "step_cluster",
    "ucd_lookup",
]
