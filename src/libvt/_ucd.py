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
import re
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

SAFE_NARROW_BMP_RE: t.Final[re.Pattern[str]] = re.compile(
    r"[ -~\xa0-\xa8\xaa-\xac\xaf-\u02ff\u0370-\u0482\u048a-\u0590\u05be\u05c0"
    r"\u05c3\u05c6\u05c8-\u05ff\u0606-\u060f\u061b\u061d-\u064a\u0660-\u066f\u"
    r"0671-\u06d5\u06de\u06e5\u06e6\u06e9\u06ee-\u070e\u0710\u0712-\u072f\u074"
    r"b-\u07a5\u07b1-\u07ea\u07f4-\u07fc\u07fe-\u0815\u081a\u0824\u0828\u082e-"
    r"\u0858\u085c-\u088f\u0892-\u0896\u08a0-\u08c9\u0904-\u0914\u093d\u0950\u"
    r"0960\u0961\u0964-\u0977\u0980\u0984-\u0994\u09a9\u09b1\u09b3-\u09b5\u09b"
    r"a\u09bb\u09bd\u09c5\u09c6\u09c9\u09ca\u09ce-\u09d6\u09d8-\u09db\u09de\u0"
    r"9e0\u09e1\u09e4-\u09ef\u09f2-\u09fd\u09ff\u0a00\u0a04-\u0a3b\u0a3d\u0a43"
    r"-\u0a46\u0a49\u0a4a\u0a4e-\u0a50\u0a52-\u0a6f\u0a72-\u0a74\u0a76-\u0a80"
    r"\u0a84-\u0a94\u0aa9\u0ab1\u0ab4\u0aba\u0abb\u0abd\u0ac6\u0aca\u0ace-\u0a"
    r"e1\u0ae4-\u0af8\u0b00\u0b04-\u0b14\u0b29\u0b31\u0b34\u0b3a\u0b3b\u0b3d\u"
    r"0b45\u0b46\u0b49\u0b4a\u0b4e-\u0b54\u0b58-\u0b5b\u0b5e\u0b60\u0b61\u0b64"
    r"-\u0b70\u0b72-\u0b81\u0b83-\u0bbd\u0bc3-\u0bc5\u0bc9\u0bce-\u0bd6\u0bd8-"
    r"\u0bff\u0c05-\u0c14\u0c29\u0c3a\u0c3b\u0c3d\u0c45\u0c49\u0c4e-\u0c54\u0c"
    r"57\u0c5b-\u0c61\u0c64-\u0c80\u0c84-\u0cbb\u0cbd\u0cc5\u0cc9\u0cce-\u0cd4"
    r"\u0cd7-\u0ce1\u0ce4-\u0cf2\u0cf4-\u0cff\u0d04-\u0d14\u0d3d\u0d45\u0d49\u"
    r"0d4f-\u0d56\u0d58-\u0d61\u0d64-\u0d80\u0d84-\u0dc9\u0dcb-\u0dce\u0dd5\u0"
    r"dd7\u0de0-\u0df1\u0df4-\u0e30\u0e32\u0e3b-\u0e46\u0e4f-\u0eb0\u0eb2\u0eb"
    r"d-\u0ec7\u0ecf-\u0f17\u0f1a-\u0f34\u0f36\u0f38\u0f3a-\u0f3d\u0f40-\u0f70"
    r"\u0f85\u0f88-\u0f8c\u0f98\u0fbd-\u0fc5\u0fc7-\u0fff\u1040-\u104f\u1090-"
    r"\u1099\u109e-\u10ff\u1200-\u135c\u1360-\u1711\u1716-\u1731\u1735-\u1751"
    r"\u1754-\u1771\u1774-\u177f\u17d4-\u17dc\u17de-\u180a\u1810-\u1884\u1887-"
    r"\u18a8\u18aa-\u191f\u192c-\u192f\u193c-\u1a16\u1a1c-\u1a1f\u1a5f\u1a7d\u"
    r"1a7e\u1a80-\u1aaf\u1ade\u1adf\u1aec-\u1aff\u1b05-\u1b0a\u1b0d-\u1b12\u1b"
    r"4d-\u1b6a\u1b74-\u1b7f\u1bb0-\u1bba\u1bbe-\u1be5\u1bf4-\u1c23\u1c38-\u1c"
    r"cf\u1cd3\u1ce9-\u1cec\u1cee-\u1cf3\u1cf5\u1cf6\u1cfa-\u1dbf\u1e00-\u200a"
    r"\u2010-\u2027\u202f-\u203b\u203d-\u2048\u204a-\u205f\u2070-\u20cf\u20f1-"
    r"\u2121\u2123-\u2138\u213a-\u2193\u219a-\u21a8\u21ab-\u2319\u231c-\u2327"
    r"\u232b-\u23ce\u23d0-\u23e8\u23f4-\u23f7\u23fb-\u24c1\u24c3-\u25a9\u25ac-"
    r"\u25b5\u25b7-\u25bf\u25c1-\u25fa\u25ff\u2605-\u260d\u260f\u2610\u2612\u2"
    r"613\u2616\u2617\u2619-\u261c\u261e\u261f\u2621\u2624\u2625\u2627-\u2629"
    r"\u262b-\u262d\u263b-\u263f\u2641\u2643-\u2647\u2654-\u265e\u2661\u2662\u"
    r"2664\u2667\u2669-\u267a\u267c\u267d\u2680-\u2689\u2690\u2691\u2698\u269a"
    r"\u269d-\u269f\u26a2-\u26a6\u26a8\u26a9\u26ac-\u26af\u26b2-\u26bc\u26bf-"
    r"\u26c3\u26c6\u26c7\u26c9-\u26cd\u26d0\u26d2\u26d5-\u26e8\u26eb-\u26ef\u2"
    r"6f6\u26fb\u26fc\u26fe-\u2701\u2703\u2704\u2706\u2707\u270e\u2710\u2711\u"
    r"2713\u2715\u2717-\u271c\u271e-\u2720\u2722-\u2727\u2729-\u2732\u2735-\u2"
    r"743\u2745\u2746\u2748-\u274b\u274d\u274f-\u2752\u2756\u2758-\u2762\u2765"
    r"-\u2794\u2798-\u27a0\u27a2-\u27af\u27b1-\u27be\u27c0-\u2933\u2936-\u2b04"
    r"\u2b08-\u2b1a\u2b1d-\u2b4f\u2b51-\u2b54\u2b56-\u2cee\u2cf2-\u2d7e\u2d80-"
    r"\u2ddf\u2e00-\u2e7f\u2e9a\u2ef4-\u2eff\u2fd6-\u2fef\u303f\u3040\u3097\u3"
    r"098\u3100-\u3104\u3130\u318f\u31e6-\u31ee\u321f\u3248-\u324f\ua48d-\ua48"
    r"f\ua4c7-\ua66e\ua673\ua67e-\ua69d\ua6a0-\ua6ef\ua6f2-\ua801\ua803-\ua805"
    r"\ua807-\ua80a\ua80c-\ua822\ua828-\ua82b\ua82d-\ua87f\ua882-\ua8b3\ua8c6-"
    r"\ua8df\ua8f2-\ua8fe\ua900-\ua925\ua92e-\ua946\ua954-\ua95f\ua97d-\ua97f"
    r"\ua984-\ua988\ua98c-\ua98e\ua9c1-\ua9df\ua9e6\ua9f0-\ua9f9\ua9ff-\uaa28"
    r"\uaa37-\uaa42\uaa44-\uaa4b\uaa4e-\uaa5f\uaa70\uaa74-\uaa79\uaa80-\uaaaf"
    r"\uaab1\uaab5\uaab6\uaab9-\uaabd\uaac0\uaac2-\uaadf\uaaf0-\uaaf4\uaaf7-\u"
    r"abbf\uabdb-\uabe2\uabeb\uabee-\uabff\ud7a4-\ud7af\ud7c7-\ud7ca\ud7fc-\ud"
    r"7ff\ue000-\uf8ff\ufb00-\ufb1d\ufb1f-\ufdff\ufe1a-\ufe1f\ufe53\ufe67\ufe6"
    r"c-\ufefe\uff00\uff61-\uff9d\uffa0-\uffdf\uffe7-\uffef\ufffc-\uffff]+"
)

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
    "SAFE_NARROW_BMP_RE",
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
