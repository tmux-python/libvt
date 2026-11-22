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
import typing as t
import zlib

if t.TYPE_CHECKING:
    from libvt.config import Profile


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
    "c-rk<XPe_R5FVQqEU*hl=O7EZ1C9fZcJ$tR3%&Q=d;gSwD0uimIEfugvLws4?6|yspE!xFIx`xLI"
    "vu6tiW0<h3hfZ1Q|UC?rPJvQI+ONjpU$GQ=}_|ioRzDH&ZYC{e7b-xq>Jd{)sw??NpioGW^_Q8(d"
    "Ef+o_vnbQM!Vzq^sx{T}{X7n$hFhB-hs^pA&RF-9R_eO>{Gzq+94#x{YoR_U{h5Q$ti4%hcYr`gJ"
    "$mlics6`{;hf#RIGRgY*zROpnl`^cX!(PtcR}6g^GP(6jU$Jx?#toL;1tykmX2MoRtgO5w??^jdO"
    "%J^8#*o1bseTLIGcb`sJ%^seE}dtUzEPd*>ehxC#1^keDwCrMsEO};;)&*_Wg`%C&tar-rWL*LSO"
    "^gaDRKhjV1GyOupCeMFc;r9uYOu&vmK!b_%KLP*%00000000000001BwD^<$qQ4ygg#Z8m#)&-xc"
    "31Zu<2xN`I6F$HJIe1OdY$`fFoepou(MwP&{v3&0~`jbmIBZN=t4tA=|{eIox7O?|J*0m&2B$vUL"
    "Mj|C2^)bi4<81qsL~ADLAECyFYpHNrk8#YGl!6`nxvs)}?-|M%V6ZoLS?Vh<l`J5e&=tmXzZw6w|"
    "WCe0A5Up@)@gJwFuvi!`24`Hu|$)exdh%`fLaN-2_k&z)sf4%wCe#U^%JXukXpE#or(Wy#mec=P("
    "l9ZqQZkBtALg3K1>KTnH||7CgfD*?LR=3C0YNHu|<{HyxbSN<c1{BPSvT<1QgK2LvVa?wRfg(J2w"
    "{9jmfcBJWh-GZ1KrF>z~XwB+jU-@rC{|Q9~gwuW(P7UUcjXQM*@3{V7D4HkFtt$U>I`EYLA@bi(A"
    "ITJw>kRH)YGtjuz1$q}w3^4*duz(SyDaxP|MgQPq)(S}>2CMv)>nCJzP*u&ZSZ^13YM3N#v*w>m$"
    "cfVQu=aVc`YK+*k}`Pe#x$cvo@kMWcgZ$Y)Vu1ehk{_*uZhTXE-r1481oT<5S($pxfe<2-XMGRsC"
    "mf@o-B|XKu}#`a{L4#_ZNwSjW3Jsqz&{n}`YO1}^nwa>cIC<(ez~ozSX7<+j@Iu;>ijph2TVH&SZ"
    "13x{^KwZBAXb!S_1+nSD`n@0fkKGrbxpN&K7I?mKf+~)YVEYVHnQb;8utFeV67tM2MA5RdHHPKy9"
    "Qqh(v@)~--zU}PShoz3C7OfKS0Q0YX^x_^JqW^5`8=Tlru(zAR;gZ#OV@zoVDPH&bjETud2dQK&y"
    "6y{@gmRao)>$!&9i=y>7<zp>C2PZ-6^B-cv%4zCpzOf84D`IN@Z~exRti%s?yA*Te&6VPW_ZgY^&"
    "OL9Ar*zVm`a08d+8{lbe;-Up~P9jRnnWe%c;Z^4y$3XLj7pfwrZ{n^rcveFZP!cJw<74kaIguQ>m"
    "v~buUqq75QY~Hff<&vx&6aFT;tMDizkyJF&T}a>(4i>c5?YP6YbZ!fD<D2wdtWSXKZ4b7VT9&(!+"
    "|r!E0;4b=0sU4+a(&@Uc_mhYyJ^-IjG?pQoH3CuL^7-g!h@P|#EVe5zC(WmG26N_CM4FG^q)r|eO"
    "@Bp8cD{ZuWhtPhmvPYVz-7k{Q{U2J-3h!ubJTF0AS!&kDBU<f?3Y8nzJ_q$zj;iZd%yY9t?s!$4C"
    "(bMF^5dFGzK$cZ?6qR|({_)foKyEKeA8U^YWa8XMx**!uNi5J0(#E${TWr=ECTVrxF$i>ZOvs32#"
    "<RD#9aS!X)ht)y<tw$?X>`Z@CSeJ2Y>JffA9x?@CSeJ2Y<uz2YQ6@XKP<q+dVGx)!n{txTbwgd-g"
    "0)Hg;4tN?pf)d-IGUjHFk>-Uwy@Z;yKaB<R=t_2T>e0>-f~3V^jQ0JvDs)&VW@@n`qx2gP|5^=H8"
    "pS0rLaLbo&hx|nH+QVyUjib2$aqPOYlGKEvt7rf)sKf7LyE}Z32w0`<2H_rTBzL}NS@Uss25%QD^"
    ")2_txQr@mlcUrWTOIQAR2c?V~ccEX;(mYU3I+V{t6a&^Pd5*bu%Bx;BVK3B|YqMlkEC0On^C9!|K"
    "jrgZ)$@P-&%DWenP^Z(6Wf}BD7}X~!6N6FexjhdQh-Y<s+Omqiovf!v+F=vONd}vy>BH{*ZEvJp8"
    "<RFk6l0OC!F&DoF7A}AFt#K>uGLd%cJe2SCsu}!`0$}bo6QbN4HQHM!PtwIr<}?!=_v6OWT&OK9C"
    "6Ck|ej;<W>@|&3{urTpOpK$|ud)YKeJC`f^HyPT_$B_gPyQv+SQ0lDDfsk-m*C=zSf2`+D?$T*@|"
    "r!HpKrbnPBfpIl_zmFb`-6p}5LjHi4+@(KXHz5MHP$31q?X?l8dv3DTv;~rnXFR;d53B*=s2LNr@"
    "MRJQ3R04oLl+ixu2=D`F1IIVt>N<L0=-R7%=iIO`Yp?!NC_i(>u79_ALIlv~mCo3&9+Cr$8MYlXq"
    "M+yZl%=6*`pZKZ$oWA4fEr;mJu7+t_SpJxL!f&2kSZWlPp2$b#Zjj+Nez(_@T3a8KnJvc+dknG{O"
    "+>;x7Y7o#^pC_MAWWphHcTf=4bj^b;a3yV;X+|fZ<6GY*5te;6G#lRG3apBb@w)EDXPWhI|4o^#i"
    "fMA!gTDC}g|JeU-UzaV0DTtEYeXU=geJZcY8wy@-Y~)6#XAsv}<JkJ-aC_M<ZI9@n+YoN_GvCVTi"
    "={88-8v)v~ypDVV5tx)8h_Af0%OoyS=usGT}Sgq7BNnfyy-YDjhS*cf+CMNkd(6C6~)CDWc={lt;"
    "pVDXl#u@DN@p{8N{aUb2SDtT$YD}SKEglDO)ig$Ce9ub!G0)h{^2V&v$8wiTd8iO}3uVz>E9WjYd"
    "GUm$hSxNc!y=Gf=TA~Qz-cRJ?n|r;rnzuiDa`SdDWNfAEZWoHWvMl^-tMvImKjSht3f$ds8@~Qs2"
    "q}O06C><y{$<0hS!qS#B3a&i2XlCc-U|G`;x0~?RTH_wb_0R**^BAZ#Djez5MiIaINnD6c}>-y4C"
    "XOY~Q-{XKCBGEp8~9yUeYR_P4*og$viDFRYK7{2H+3dC>a}AO?>8i9-OR#y$Wz@IN@`B5("
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
    has_spacing: int,
    multi_scalar: int,
) -> int:
    return (
        _ACTIVE_BIT
        | (gcb << 1)
        | (ri_odd << 5)
        | (ep_state << 6)
        | (incb_state << 8)
        | (vs_base << 10)
        | (has_spacing << 11)
        | (multi_scalar << 12)
    )


def init_cluster_state(cp: int, *, mode_2027: bool = True) -> int:
    """Return initial packed cluster state for base codepoint *cp*."""
    entry = ucd_lookup(cp)
    w = entry & 0b11
    gcb = (entry >> 2) & 0x0F
    incb = (entry >> 6) & 0x03
    ep = 1 if (entry & EXT_PICT_BIT) else 0
    vs_b = 1 if (entry & EMOJI_VS_BASE_BIT) else 0
    ri_odd = 1 if (mode_2027 and gcb == GCB_REGIONAL_INDICATOR) else 0
    incb_st = 1 if incb == INCB_CONSONANT else 0
    has_sp = 1 if w >= 1 else 0
    return _pack_state(gcb, ri_odd, ep, incb_st, vs_b, has_sp, 0)


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
    vs15_narrows: bool = True,
    vs16_always_wide: bool = False,
) -> tuple[bool, int, int]:
    """Evaluate UAX #29 continuation between *prev_state* and codepoint *cp*.

    Returns
    -------
    tuple[bool, int, int]
        ``(extends_previous, next_state, width_override)`` where
        ``width_override`` is ``-1`` (keep current cluster width), ``1``
        (narrow via ``VS15`` on an emoji variation base or first narrow glyph
        after ``Prepend``), or ``2`` (widen via ``VS16``, spacing continuation,
        emoji modifier, ZWJ pictographic, or Regional Indicator pair).
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
        has_spacing = (prev_state >> 11) & 0x01
        multi_scalar = (prev_state >> 12) & 0x01

        if not mode_2027:
            if gcb == GCB_EXTEND:
                return (
                    True,
                    _pack_state(gcb, 0, ep_state, incb_state, 0, has_spacing, 1),
                    -1,
                )
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
            if cp == 0xFE0F and (vs_base or (vs16_always_wide and has_spacing)):
                w_override = 2
            elif cp == 0xFE0E and vs_base and vs15_narrows and not multi_scalar:
                w_override = 1
            elif not has_spacing and w >= 1:
                w_override = w
            elif has_spacing and (
                w >= 1
                or gcb == GCB_SPACING_MARK
                or ((entry & EMOJI_MOD_BIT) and ep_state == 1)
                or (ep_state == 2 and ep)
                or (ri_odd == 1 and gcb == GCB_REGIONAL_INDICATOR)
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

            next_vs = 1 if (entry & EMOJI_VS_BASE_BIT) else 0
            next_sp = 1 if (has_spacing or w >= 1 or gcb == GCB_SPACING_MARK) else 0
            next_multi = 0 if (not has_spacing and w >= 1) else 1
            return (
                True,
                _pack_state(
                    gcb,
                    next_ri,
                    next_ep,
                    next_incb,
                    next_vs,
                    next_sp,
                    next_multi,
                ),
                w_override,
            )

    if w == 0 and gcb in (
        GCB_CR,
        GCB_LF,
        GCB_CONTROL,
        GCB_EXTEND,
        GCB_ZWJ,
        GCB_SPACING_MARK,
        GCB_V,
        GCB_T,
    ):
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


def display_width(
    text: str,
    *,
    mode_2027: bool = True,
    profile: Profile | None = None,
) -> int:
    """Return rendered terminal column width of *text*."""
    if text.isascii():
        return sum(1 for ch in text if " " <= ch <= "~")
    vs15_narrows = profile.vs15_narrows_wide_emoji if profile is not None else True
    vs16_wide = profile.vs16_always_wide if profile is not None else False
    total = 0
    cur_w = 0
    state = CLUSTER_NONE
    for ch in text:
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E and state == CLUSTER_NONE:
            cur_w = 1
            state = init_cluster_state(cp, mode_2027=mode_2027)
            continue
        extends, state, w_override = step_cluster(
            state,
            cp,
            mode_2027=mode_2027,
            vs15_narrows=vs15_narrows,
            vs16_always_wide=vs16_wide,
        )
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
