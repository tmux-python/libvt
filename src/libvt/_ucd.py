"""Generated two-stage Unicode 17.0.0 width and UAX #29 table.

Regenerate with ``uv run python scripts/generate_ucd.py``.

Packed 16-bit entry layout in ``_STAGE2``:
- bits ``0..1``: cell width (``0``, ``1``, or ``2``)
- bits ``2..5``: ``Grapheme_Cluster_Break`` property ID (``0..13``)
- bits ``6..7``: ``Indic_Conjunct_Break`` property ID (``0..3``)
- bit ``8``: ``Extended_Pictographic``
- bit ``9``: ``Emoji_Modifier``
- bit ``10``: ``emoji-variation-sequences.txt`` ``FE0E``/``FE0F`` base
- bit ``11``: BiDi control / spoofing codepoint
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


GCB_OTHER = 0
GCB_CR = 1
GCB_LF = 2
GCB_CONTROL = 3
GCB_EXTEND = 4
GCB_ZWJ = 5
GCB_REGIONAL_INDICATOR = 6
GCB_PREPEND = 7
GCB_SPACING_MARK = 8
GCB_L = 9
GCB_V = 10
GCB_T = 11
GCB_LV = 12
GCB_LVT = 13

INCB_NONE = 0
INCB_LINKER = 1
INCB_CONSONANT = 2
INCB_EXTEND = 3

EXT_PICT_BIT = 1 << 8
EMOJI_MOD_BIT = 1 << 9
EMOJI_VS_BASE_BIT = 1 << 10
BIDI_SPOOF_BIT = 1 << 11

CLUSTER_NONE = 0
_ACTIVE_BIT = 1

UNICODE_VERSION = "17.0.0"

_PACKED_B85 = (
    "c-rk<XWOJS6usa^SAAbydmWWkS65xj+I#P9*4}&Xy?&a12>bYhWEdFI2@n{{;XK34KoW9GZf-&f9"
    "j5dMQiOC09Uw-h(rI*%PNy^IOxmM;I*ZPxL+Sl<vQPn?OXtz~bOBvR7tzJpl}qT-^n4jzP7At%u1"
    "tSlm3|^ROjpx2bS)jB>*#tqI(giXQhj6kxrvU^&2$UhO1IH*x}ENzJLxWO|L&%HBm}jw&h5R~ulw"
    "l!^!xxlNDm1i9?s5>(4+JiJx)*1lk^llP0!G?^c+1;FVKth61_}IIzg{EKlRlHId#Kp%9YpYjr9C"
    "x`gyCdJm02wJml@&G^O|Gea)Q@oZ^3&em<g)=@a4Vr`+$)Qd&PxzrUa_>8tenYx+hA`z?J(-_sBD"
    "BmG1_(=YTZ{YJm1*Z;`y=L||FV8&nI!OZ0!000000000000000001yq{7wJRzZQT)0002f#GVERv"
    "-1J%JsEjA2ZU4)2)|Vfn)-Usq{=qm``cdtFjT}72P6#CEd}5Q(1(Q#@(+LRo4N%Dcj{Bm>EJMUUY"
    "*iZHE|(b2?Sm8W5*V)IXI_UdpNYX<RWgIYGANs^1Uu;>(f5g<7@vl&Z2cq6glK-<qhlStvMHEQ1i"
    "0EGCRxMkkcxyo*UHoasC`%^aq;%VhTm+O9c9ZoFm2eQj>>7$W7ot|7!bmlLc^<zi%Dq<<D!rT*lk"
    "iKlXHd>wlpAA1FGj>VKIRll-e_bSnX}-Hvudf0A1QH~n9SsjvTmMgMp0BPJR3CHJHJccB-y^6#%4"
    "nxE!J{wssc4kUXodk{;lmajAxtzA9rs{dWszc|x@WJ`aNNjS+$My$v_X<YsztL=z-uK!ngj^+AK)"
    "c>WXMVff9f5VW!n?2$=#Mc=G-0Z<_=_@qd#cQ5o@2!sh*y@c8nf~Uvin&jpdTDR>$lh0VYreZT6T"
    "6W2suS@1izKR-xy9YI=IZ3Hj8)bmvCz8G#Wl4Y`PwLu+98|#uV&D*cx2=l+Kk#`Bg5Ex!|{HqFHR"
    "?|;wL-tIjZBYdV*RL#*!}|arL5zW$)g?=DB;bDqp^=NjyW_5OIB(k>J;tdL0S<ov^B7mA2Shxatj"
    "&#)1Y}FJY{0uc<m0Yu=ZSd3>EM{kr*!tZ$8c)PD(=yCm=VETogr32hs)%lF^BMz__=m@64;YL?p;"
    "r_Jl=qAx$@di0|$x0BBGT=iwt(g?fXyp`kDhb6vCRlO3(0OPM+?BX6BqW?_u8?4w*v3Ib5B_wNbN"
    "1M|E+;i>oGiIhAJ>-&g7<wpR9I8W3+Gd3;w3ObELfG})l&r0EG6`h}i?gW5;Oxky3=F)kaFsLLRS"
    "t72ZmZQ;dEVN5rn$=k@gC!zLL!uBF_8y_^^#G+=`t6rMu{_|E9AEbS8|C75*E{7O8scGwjxy;{!+"
    "+2FLc)vIY)VIP)a+@b1COq49{_ssdCato1|3h`9u=+>vTdUYK1ZNUVJX29x{8Z=67dd6M<p1aN4&"
    "3JeRr|mK6ZN7@1!9Gxz?%xobe!0L^0U7Cz|*h9yJa`rQ_VZ8@1DWX5nT9GwOh7I%s|)m8fA=FYh7"
    "!}$0!@cM~`Hj4%Tz@%!%;d^*Mb?Z3jr0qL~_H&&z(&EPbBIVfsvGuI<H8(!0KvP|6*T)lg+85=E8"
    "rwPt&1a5^^H=m^vwg03rH&I{f1hZ{b8!~)%4rn6pE33>GUO?>kHXiDWv@rSeKi^}PO#-xA3e<S`i"
    "!D&jy=g=os%HiwmK_H0ezwQ`U-0O8*%GNMUHxPobGA_#6vv9Lp;PoJj6ph#6vv9Lp;PAS3K~;Pdr"
    "omy58z>MXdhTeZw*DW8R}@fvR^$Rd1=A#P4pLQGhq;wXipV5x~3SULWf!)f?Zd-rrX+jdf7~%zXh"
    "M#b&+^cu~H8w(owRj-&9gD?srSj7aErq+cH+Em6t=l*M=y^<X^MeD#^bIope<=iNX1o{b(`%VXU6"
    "?x&G<<nQ{<qQZw?^e7LXyIkm2C0<s_cC)+FsxvBW^hZ6EGW{y_o00AMJ*M5qa1M1|MI>l_qS;{!E"
    "!ehtt=Fu2<-h;^A<O+g`TbwT{eKf}kE?ACkB$dbJTc7~DCP<H-W4xV<W*2r?EIQ+_dwn3B(SKE=U"
    "6-!`@R5i18X-1x>b1>I-hK3X`rn-RmeLW2hvVSZFW-~f2p6Y=EvII+>%LATUu=WZo0snVQ%q2JBI"
    "cCNA^%xTE94G`1U8^=D3TV`o_17vJW)EwIuOvHhw3*sImNY{cv6Ee#-AOXR0NZ75OVU;X8*54)(p"
    "a@Xm60UP#%l0!N1Sc0ugxkW16J#*MhFwGwGPp85Jcx<0u;yDHOSAROX7mO`e{i1ZZzTzmO9<t}nq"
    "L8tBR&1&yJ*~dM-eqUgNy$ZxT9UTC4;TQ2OR&WUbhEPU_U?U(8pbH$`ev9+yfw5b!>Xmc-LT|l>%"
    "b{}575)8e50)M75CIH%rZd*7hvooNhG_+j!0Y}!Re5Ne|LRmmvVRZ&phlQX&q~?9J+(euAE=)`v<"
    "hg7B%fgxbN>sdZ=I?%)km3vOzQ9pd_eoR>m5!Z?>_5)bN$}uv)o3F#0|MN_Av{#@tLk(ojRItO79"
    ";4V0_vG9~AXE<PRMH7v@vb#8&x37slT{1D!xe{Xoofip4P%4w;VXSXCixQt`{d`uQI}Sil<H+i*W"
    "St7xe+9UYfYeS~ZJm^n>tKPq$XaovP0ImhyEGN&&+KM1Wfn`7c|IYKj7232;_zq|}F8JbeV!eHlM"
    "jdH^{d%?DLL#2`#xmT4Z#>Lj~P~~qPf>lwv$!RX9{MyN|fSEr|cbH>b12*|8=@z)g1a3Bxv4>D?p"
    "UAZLSw%eN_%yw~F{AdeGUQqxYNXvkSv1$mWr!^;jxf~ll4Y`Z1bpc7O6~_(eFZ6eg_psk5OyzxS*"
    "|i6HKu)vbk%!V>I|K?d(6}_Z3$)#ILDNFRhy0|A-)FSb1K%`f@W`J&3R4CB=MQ}|5Jp6|5o0wIEL"
    "1KcgbIu?N^`eV^{vxlaJrS%`SSk>h4#8G1sp<EuYr*t<8Q`wvD@zhG4m?!n#<0^F16maLoF^{J7c"
    "A0bB0}z25@y$gw|j#K5Gn4*(YY59z2SMg"
)


def _unpack_tables() -> tuple[array.array[int], array.array[int]]:
    raw = zlib.decompress(base64.b85decode(_PACKED_B85))
    s1_len = int.from_bytes(raw[:4], "little")
    s1 = array.array("H")
    s1.frombytes(raw[4 : 4 + s1_len])
    s2 = array.array("H")
    s2.frombytes(raw[4 + s1_len :])
    if sys.byteorder != "little":
        s1.byteswap()
        s2.byteswap()
    return s1, s2


_STAGE1, _STAGE2 = _unpack_tables()


def ucd_lookup(cp: int) -> int:
    """Return packed 16-bit width and UAX #29 property word for codepoint *cp*."""
    if 0 <= cp <= 0x10FFFF:
        return _STAGE2[(_STAGE1[cp >> 8] << 8) | (cp & 0xFF)]
    return GCB_CONTROL << 2


def _pack_state(
    gcb: int,
    ri_odd: int,
    ep_state: int,
    incb_state: int,
    vs_base: int,
) -> int:
    return (
        _ACTIVE_BIT
        | (gcb << 1)
        | (ri_odd << 5)
        | (ep_state << 6)
        | (incb_state << 8)
        | (vs_base << 10)
    )


def init_cluster_state(cp: int, *, mode_2027: bool = True) -> int:
    """Return initial packed cluster state for base codepoint *cp*."""
    entry = ucd_lookup(cp)
    gcb = (entry >> 2) & 0x0F
    incb = (entry >> 6) & 0x03
    ep = 1 if (entry & EXT_PICT_BIT) else 0
    vs_b = 1 if (entry & EMOJI_VS_BASE_BIT) else 0
    ri_odd = 1 if (mode_2027 and gcb == GCB_REGIONAL_INDICATOR) else 0
    incb_st = 1 if incb == INCB_CONSONANT else 0
    return _pack_state(gcb, ri_odd, ep, incb_st, vs_b)


def char_width(ch: str) -> int:
    """Return default terminal column width (``0``, ``1``, or ``2``) for *ch*."""
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:
        return 1
    return ucd_lookup(cp) & 0b11


def step_cluster(
    prev_state: int,
    cp: int,
    *,
    mode_2027: bool,
) -> tuple[bool, int, int]:
    """Evaluate UAX #29 continuation between *prev_state* and codepoint *cp*.

    Returns
    -------
    tuple[bool, int, int]
        ``(extends_previous, next_state, width_override)`` where
        ``width_override`` is ``-1`` (keep current cluster width), ``1``
        (narrow via ``VS15`` on an emoji variation base), or ``2`` (widen via
        ``VS16`` on an emoji variation base, emoji modifier, ZWJ pictographic,
        Regional Indicator pair, or wide glyph after ``Prepend``).
    """
    entry = ucd_lookup(cp)
    gcb = (entry >> 2) & 0x0F
    incb = (entry >> 6) & 0x03
    ep = bool(entry & EXT_PICT_BIT)
    w = entry & 0b11

    if prev_state & _ACTIVE_BIT:
        prev_g = (prev_state >> 1) & 0x0F
        ri_odd = (prev_state >> 5) & 0x01
        ep_state = (prev_state >> 6) & 0x03
        incb_state = (prev_state >> 8) & 0x03
        vs_base = (prev_state >> 10) & 0x01

        if not mode_2027:
            if gcb == GCB_EXTEND:
                return True, _pack_state(gcb, 0, ep_state, incb_state, 0), -1
            if w == 0:
                return False, CLUSTER_NONE, -1
            return False, init_cluster_state(cp, mode_2027=False), -1

        joined = False
        if prev_g == GCB_CR and gcb == GCB_LF:
            joined = True
        elif prev_g in (GCB_CR, GCB_LF, GCB_CONTROL) or gcb in (
            GCB_CR,
            GCB_LF,
            GCB_CONTROL,
        ):
            joined = False
        elif (
            (prev_g == GCB_L and gcb in (GCB_L, GCB_V, GCB_LV, GCB_LVT))
            or (prev_g in (GCB_LV, GCB_V) and gcb in (GCB_V, GCB_T))
            or (prev_g in (GCB_LVT, GCB_T) and gcb == GCB_T)
            or gcb in (GCB_EXTEND, GCB_ZWJ, GCB_SPACING_MARK)
            or prev_g == GCB_PREPEND
            or (incb_state == 2 and incb == INCB_CONSONANT)
            or (ep_state == 2 and ep)
            or (ri_odd == 1 and gcb == GCB_REGIONAL_INDICATOR)
        ):
            joined = True

        if joined:
            w_override = -1
            if cp == 0xFE0F and vs_base:
                w_override = 2
            elif cp == 0xFE0E and vs_base:
                w_override = 1
            elif (
                ((entry & EMOJI_MOD_BIT) and ep_state == 1)
                or (ep_state == 2 and ep)
                or (ri_odd == 1 and gcb == GCB_REGIONAL_INDICATOR)
                or (prev_g == GCB_PREPEND and w == 2)
            ):
                w_override = 2

            next_ri = (ri_odd ^ 1) if gcb == GCB_REGIONAL_INDICATOR else 0
            if ep_state == 1 and gcb == GCB_EXTEND:
                next_ep = 1
            elif ep_state == 1 and gcb == GCB_ZWJ:
                next_ep = 2
            elif ep:
                next_ep = 1
            else:
                next_ep = 0

            if incb == INCB_CONSONANT:
                next_incb = 1
            elif incb_state in (1, 2) and incb == INCB_LINKER:
                next_incb = 2
            elif incb_state in (1, 2) and incb == INCB_EXTEND:
                next_incb = incb_state
            else:
                next_incb = 0

            next_vs = (
                1 if (prev_g == GCB_PREPEND and (entry & EMOJI_VS_BASE_BIT)) else 0
            )
            return (
                True,
                _pack_state(gcb, next_ri, next_ep, next_incb, next_vs),
                w_override,
            )

    if w == 0 and gcb in (GCB_CR, GCB_LF, GCB_CONTROL, GCB_EXTEND, GCB_ZWJ):
        return False, CLUSTER_NONE, -1
    return False, init_cluster_state(cp, mode_2027=mode_2027), -1


def segment_graphemes(text: str) -> list[str]:
    """Segment *text* into Unicode 17.0.0 UAX #29 extended grapheme clusters."""
    if not text:
        return []
    clusters: list[str] = []
    buf: list[str] = [text[0]]
    state = init_cluster_state(ord(text[0]), mode_2027=True)
    for ch in text[1:]:
        cp = ord(ch)
        entry = ucd_lookup(cp)
        gcb = (entry >> 2) & 0x0F
        prev_g = (state >> 1) & 0x0F
        if prev_g in (GCB_CR, GCB_LF, GCB_CONTROL) and not (
            prev_g == GCB_CR and gcb == GCB_LF
        ):
            clusters.append("".join(buf))
            buf = [ch]
            state = init_cluster_state(cp, mode_2027=True)
            continue
        extends, next_state, _ = step_cluster(state, cp, mode_2027=True)
        if extends:
            buf.append(ch)
            state = next_state
        else:
            clusters.append("".join(buf))
            buf = [ch]
            state = init_cluster_state(cp, mode_2027=True)
    clusters.append("".join(buf))
    return clusters


def display_width(text: str, *, mode_2027: bool = True) -> int:
    """Return rendered terminal column width of *text*."""
    if text.isascii():
        return sum(1 for ch in text if " " <= ch <= "~")
    total = 0
    cur_w = 0
    state = CLUSTER_NONE
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E and state == CLUSTER_NONE:
            cur_w = 1
            state = init_cluster_state(cp, mode_2027=mode_2027)
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
    "CLUSTER_NONE",
    "UNICODE_VERSION",
    "WidthRole",
    "char_width",
    "display_width",
    "init_cluster_state",
    "sanitize_terminal_text",
    "segment_graphemes",
    "step_cluster",
    "ucd_lookup",
]
