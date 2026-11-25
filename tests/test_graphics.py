"""Tests for ``libvt.graphics`` zero-dependency ``SIXEL`` and Kitty ``APC G``."""

from __future__ import annotations

import base64
import struct
import time
import typing as t
import zlib

import pytest

import libvt
from libvt.config import Profile
from libvt.events import KittyGraphic
from libvt.graphics import GraphicsProtocol, ZLayer


def _minimal_png(width: int, height: int) -> bytes:
    """Construct a minimal PNG header with ``IHDR`` dimensions ``(width, height)``."""
    ihdr_body = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_body) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr_body + ihdr_crc


def test_kitty_chunked_upload_validation_and_relative_placements() -> None:
    """Kitty ``APC G`` validates uploads, silences unnumbered IDs, and links parents."""
    png_bytes = _minimal_png(16, 32)
    z_payload = base64.b64encode(zlib.compress(png_bytes))
    part1, part2 = z_payload[:12], z_payload[12:]

    term = libvt.Terminal(rows=6, cols=10, scrollback=10)
    term.feed(b"\x1b[2;3H\x1b_Ga=T,f=100,o=z,i=7,p=1,z=-5,m=1;" + part1 + b"\x1b\\")
    assert len(term.placements) == 0
    term.feed(b"\x1b_Gm=0;" + part2 + b"\x1b\\")
    assert b"\x1b_Gi=7,p=1;OK\x1b\\" in term.drain_replies()
    assert len(term.placements) == 1
    kp = term.placements[0]
    assert not hasattr(kp, "row")
    assert (kp.protocol, kp.abs_row, kp.col, kp.rows, kp.cols, kp.layer) == (
        GraphicsProtocol.KITTY,
        1,
        2,
        2,
        2,
        ZLayer.BELOW_TEXT,
    )
    assert term.snapshot().viewport_row(kp.abs_row) == 1
    assert (term.cursor.row, term.cursor.col) == (2, 4)

    # U=0 overlay survives text overwrite; d=y with missing y=0 is a safe no-op.
    term.feed(b"\x1b[2;3HOVER\x1b[K\x1b_Ga=d,d=y\x1b\\")
    assert len(term.placements) == 1

    # Retransmitting i=7 clears stale placements; unnumbered upload stays silent.
    raw_1cell = base64.b64encode(b"\xff" * (8 * 16 * 3))
    term.feed(b"\x1b[1;1H\x1b_Ga=t,f=24,s=8,v=16,i=7;" + raw_1cell + b"\x1b\\")
    assert (len(term.placements), b"\x1b_Gi=7;OK\x1b\\" in term.drain_replies()) == (
        0,
        True,
    )
    term.feed(b"\x1b[1;1H\x1b_Ga=T,f=24,s=8,v=16;" + raw_1cell + b"\x1b\\")
    assert (term.drain_replies(), len(term.placements)) == (b"", 1)

    # Reject missing-i query, mutually exclusive i+I, truncated raw/zlib, and t=f.
    term.feed(b"\x1b_Ga=q,f=24,s=1,v=1;" + base64.b64encode(b"abc") + b"\x1b\\")
    assert term.drain_replies() == b""
    for bad_pkt in (
        b"\x1b_Ga=t,i=2,I=3,f=24,s=1,v=1;" + base64.b64encode(b"abc") + b"\x1b\\",
        b"\x1b_Ga=t,i=8,f=24,s=8,v=16;" + base64.b64encode(b"short") + b"\x1b\\",
        b"\x1b_Ga=t,i=9,f=100,o=z;"
        + base64.b64encode(zlib.compress(png_bytes)[:-4])
        + b"\x1b\\",
        b"\x1b_Ga=T,t=f,i=99;" + base64.b64encode(b"/etc/passwd") + b"\x1b\\",
    ):
        term.feed(bad_pkt)
        assert b"EINVAL" in term.drain_replies()

    # Virtual U=1 parent rejected as non-virtual parent; real parent cascades delete.
    term.feed(b"\x1b_Ga=T,U=1,i=20,p=1,c=2,r=2,f=24,s=8,v=16;" + raw_1cell + b"\x1b\\")
    term.feed(b"\x1b_Ga=p,i=20,p=2,P=20,Q=1\x1b\\")
    assert b"ENOPARENT" in term.drain_replies()
    term.feed(b"\x1b[2;2H\x1b_Ga=p,i=20,p=3,c=2,r=2\x1b\\")
    term.feed(b"\x1b_Ga=p,i=20,p=4,P=20,Q=3,H=2,V=1\x1b\\")
    term.drain_replies()
    child = [p for p in term.placements if p.image_id == 20 and p.placement_id == 4]
    assert len(child) == 1
    assert (child[0].abs_row, child[0].col) == (2, 3)
    assert term.snapshot().viewport_row(child[0].abs_row) == 2
    term.feed(b"\x1b_Ga=d,d=i,i=20,p=3\x1b\\")
    assert not any(
        p.image_id == 20 and p.placement_id in (3, 4) for p in term.placements
    )


def test_scroll_before_place_sixel_erase_policies_and_event_immutability() -> None:
    """Alt-screen scroll-before-place, ``SIXEL`` erase policies, and ``O(rows)``."""
    alt_t = libvt.Terminal(rows=4, cols=8)
    raw_2row = base64.b64encode(b"\xaa" * (16 * 32 * 3))
    alt_t.feed(b"\x1b[?1049h\x1b[4;2H\x1b_Ga=T,f=24,s=16,v=32;" + raw_2row + b"\x1b\\")
    alt_snap = alt_t.snapshot()
    assert (alt_snap.placements[0].abs_row, alt_snap.placements[0].rows) == (2, 2)
    assert alt_snap.viewport_row(alt_snap.placements[0].abs_row) == 2
    assert alt_snap.to_dict()["scrolled_total"] == 0
    assert alt_snap.to_dict()["placements"][0]["abs_row"] == 2
    assert alt_snap.to_dict()["placements"][0]["viewport_row"] == 2
    with pytest.raises(IndexError):
        alt_snap.at_abs(-1)
    with pytest.raises(IndexError):
        alt_snap.at_abs(4)

    # Tall Kitty image on alt screen crops top rows at abs_row == 0, never < 0.
    tall_raw = base64.b64encode(b"\xff" * (8 * 80 * 3))
    alt_tall = libvt.Terminal(rows=4, cols=10)
    alt_tall.feed(
        b"\x1b[?1049h\x1b[3;1H\x1b_Ga=T,f=24,s=8,v=80;" + tall_raw + b"\x1b\\"
    )
    tall_snap = alt_tall.snapshot()
    assert (tall_snap.placements[0].abs_row, tall_snap.placements[0].rows) == (0, 4)
    assert tall_snap.viewport_row(tall_snap.placements[0].abs_row) == 0
    assert tall_snap.at_abs(tall_snap.placements[0].abs_row).text == tall_snap[0].text

    # Alt-screen non-reflow resize dropping top rows crops straddling placement to 0.
    alt_rz = libvt.Terminal(rows=4, cols=10)
    alt_rz.feed(
        b"\x1b[?1049h\x1b[2;1H\x1b_Ga=T,f=24,s=16,v=32;" + raw_2row + b"\x1b\\\x1b[4;1H"
    )
    alt_rz.resize(rows=2, cols=10)
    rz_snap = alt_rz.snapshot()
    assert (rz_snap.placements[0].abs_row, rz_snap.placements[0].rows) == (0, 1)
    assert rz_snap.viewport_row(rz_snap.placements[0].abs_row) == 0

    # Tall SIXEL spans scrollback + viewport on primary, and crops at 0 on alt.
    sixel_8r = b'\x1bP0;1;0q"1;1;16;128#0!16~\x1b\\'
    prim_sixel = libvt.Terminal(rows=4, cols=10, scrollback=10)
    prim_sixel.feed(b"\x1b[4;1H" + sixel_8r)
    ps_snap = prim_sixel.snapshot()
    assert (ps_snap.placements[0].abs_row, ps_snap.placements[0].rows) == (3, 8)
    assert ps_snap.viewport_row(ps_snap.placements[0].abs_row) is None
    assert ps_snap.placements[0].occupies_abs(ps_snap.scrolled_total + 0, 0) is True

    sixel_5r = b'\x1bP0;1;0q"1;1;16;80#0!16~\x1b\\'
    alt_sixel = libvt.Terminal(rows=4, cols=10)
    alt_sixel.feed(b"\x1b[?1049h\x1b[3;1H" + sixel_5r)
    as_snap = alt_sixel.snapshot()
    assert (as_snap.placements[0].abs_row, as_snap.placements[0].rows) == (0, 3)
    assert as_snap.viewport_row(as_snap.placements[0].abs_row) == 0

    decstbm_sx = libvt.Terminal(
        rows=6, cols=10, scrollback=10, profile=Profile.tmux("next-3.9")
    )
    decstbm_sx.feed(b'\x1b[3;1H\x1bPq"1;1;20;64#0~\x1b\\\x1b[1;4r\x1b[4;1H\n\n')
    ds_snap = decstbm_sx.snapshot()
    assert len(ds_snap.placements) == 1
    assert ds_snap.placements[0].occupies_abs(ds_snap.scrolled_total + 4, 0) is True
    assert ds_snap.placements[0].occupies_abs(ds_snap.scrolled_total + 2, 0) is False

    # Spanning scrollback + DECSTBM top==0 preserves viewport/history visibility.
    span_sx = libvt.Terminal(
        rows=6, cols=10, scrollback=10, profile=Profile.tmux("next-3.9")
    )
    span_sx.feed(b'\x1b[1;4r\x1b[4;1H\x1bPq"1;1;16;80#0!16~\x1b\\\n')
    span_snap = span_sx.snapshot()
    assert span_snap.placements[0].occupies_abs(span_snap.scrolled_total - 1, 0) is True
    assert span_snap.placements[0].occupies_abs(span_snap.scrolled_total, 0) is True

    # Placement inside DECSTBM 1;4r scrolls into scrollback instead of clipping.
    hist_sx = libvt.Terminal(
        rows=6, cols=10, scrollback=10, profile=Profile.tmux("next-3.9")
    )
    hist_sx.feed(
        b'\x1b[1;4r\x1b[1;1HROW0\r\nROW1\x1b[1;1H\x1bPq"1;1;16;32#0!16~\x1b\\\x1b[4;1H\n\n'
    )
    assert len(hist_sx.snapshot().placements) == 1
    assert hist_sx.snapshot().placements[0].abs_row == 0

    rz_gfx = libvt.Terminal(rows=6, cols=10, scrollback=10)
    rz_gfx.feed(b'\x1b[2;4r\x1b[1;1H\x1bPq"1;1;20;20#0~\x1b\\')
    rz_gfx.resize(rows=6, cols=12)
    assert (rz_gfx.screen.scroll_region.top, rz_gfx.screen.scroll_region.bottom) == (
        0,
        5,
    )
    assert len(rz_gfx.snapshot().placements) == 1
    assert len(rz_gfx.snapshot().images) == 1
    rz_gfx.feed(b"\x1b[6;1H" + b"\n" * 6)
    rz_gfx.resize(rows=8, cols=12)
    assert len(rz_gfx.snapshot().placements) == 1

    pin_term = libvt.Terminal(rows=4, cols=6, scrollback=10)
    raw_1cell = base64.b64encode(b"\xff" * (8 * 16 * 3))
    pin_term.feed(
        b"012345$"
        b"\x1b]133;A\x07"
        b"\x1b_Ga=T,f=24,s=8,v=16,i=42,p=1;" + raw_1cell + b"\x1b\\"
        b"ok\x1b]133;B\x07"
    )
    assert (pin_term.prompt_zones[0].abs_row, pin_term.prompt_zones[0].col) == (1, 1)
    assert (pin_term.prompt_zones[1].abs_row, pin_term.prompt_zones[1].col) == (1, 4)
    assert (pin_term.placements[0].abs_row, pin_term.placements[0].col) == (1, 1)
    pin_term.resize(rows=4, cols=12)
    pin_snap = pin_term.snapshot()
    assert pin_snap[0].text == "012345$ ok"
    assert (pin_snap.prompt_zones[0].abs_row, pin_snap.prompt_zones[0].col) == (0, 7)
    assert (pin_snap.prompt_zones[1].abs_row, pin_snap.prompt_zones[1].col) == (0, 10)
    assert (pin_snap.placements[0].abs_row, pin_snap.placements[0].col) == (0, 7)

    # Unnumbered relative children, out-of-order chains, and U=1 replacement.
    rel_term = libvt.Terminal(rows=6, cols=6, scrollback=10)
    rel_term.feed(
        b"012345$"
        b"\x1b_Ga=T,f=24,s=8,v=16,i=1,p=1;" + raw_1cell + b"\x1b\\"
        b"\x1b_Ga=t,f=24,s=8,v=16,i=2;" + raw_1cell + b"\x1b\\"
        b"\x1b_Ga=t,f=24,s=8,v=16,i=3;" + raw_1cell + b"\x1b\\"
        b"\x1b_Ga=p,i=2,P=1,Q=1,H=1,V=0\x1b\\"
        b"\x1b_Ga=p,i=2,P=1,Q=1,H=3,V=2\x1b\\"
        b"\x1b_Ga=p,i=2,p=1,P=1,Q=1,H=1,V=1\x1b\\"
        b"\x1b_Ga=p,i=3,p=1,P=2,Q=1,H=1,V=1\x1b\\"
        b"\x1b_Ga=p,i=2,p=1,P=1,Q=1,H=1,V=1\x1b\\"
    )
    rel_term.resize(rows=6, cols=12)
    coords = {
        (p.image_id, p.placement_id, p.abs_row, p.col) for p in rel_term.placements
    }
    assert coords == {
        (1, 1, 0, 7),
        (2, 0, 0, 8),
        (2, 0, 2, 10),
        (2, 1, 1, 8),
        (3, 1, 2, 9),
    }
    rel_term.feed(b"\x1b_Ga=p,U=1,i=1,p=1\x1b\\")
    assert [p.virtual for p in rel_term.placements] == [True]

    # Width shrink clips SIXEL cols/mask; height shrink clips bottom placements/zones.
    shrink_sx = libvt.Terminal(rows=4, cols=10, profile=Profile.tmux("next-3.9"))
    shrink_sx.feed(b'\x1bPq"1;1;48;16#0!48~\x1b\\')
    shrink_sx.resize(rows=4, cols=3)
    assert shrink_sx.placements[0].cols == 3
    shrink_sx.feed(b"\x1b[1;1H\x1b[2K")
    assert shrink_sx.placements == ()

    bot_crop = libvt.Terminal(rows=6, cols=10, scrollback=10)
    bot_crop.feed(
        b"\x1b[5;1H\x1b]133;A\x07"
        b"\x1b_Ga=T,f=24,s=8,v=16,i=9,p=1,C=1;" + raw_1cell + b"\x1b\\\x1b[1;1H"
    )
    bot_crop.resize(rows=2, cols=12)
    assert bot_crop.placements == ()
    assert bot_crop.prompt_zones == ()

    tall_evict = libvt.Terminal(
        rows=4, cols=6, scrollback=2, profile=Profile.tmux("next-3.9")
    )
    tall_evict.feed(
        b'\x1b[1;1H\x1bPq"1;1;16;64#0!16~\x1b\\\x1b[1;1H0123456789AB\x1b[4;1H\n\n'
    )
    tall_evict.resize(rows=4, cols=2)
    te_snap = tall_evict.snapshot()
    assert len(te_snap.placements) == 1
    assert te_snap.at_abs(te_snap.placements[0].abs_row) is not None

    # ED 3 clips straddling placements at scrolled_total; Kitty P= rejects SIXEL IDs.
    ed3_sx = libvt.Terminal(
        rows=4, cols=6, scrollback=2, profile=Profile.tmux("next-3.9")
    )
    ed3_sx.feed(b'\x1b[1;1H\x1bPq"1;1;16;64#0!16~\x1b\\\x1b[4;1H\n\x1b[3J')
    ed3_snap = ed3_sx.snapshot()
    assert ed3_snap.placements[0].abs_row == ed3_snap.evicted_total
    assert ed3_snap.placements[0].occupies_abs(0, 0) is False
    assert ed3_snap.at_abs(ed3_snap.placements[0].abs_row) is not None
    sx_auto_id = ed3_snap.placements[0].image_id
    ed3_sx.feed(
        b"\x1b_Ga=t,f=24,s=8,v=16,i=1;"
        + raw_1cell
        + f"\x1b\\\x1b_Ga=p,i=1,p=1,P={sx_auto_id},V=1\x1b\\".encode()
    )
    assert b"ENOPARENT" in ed3_sx.drain_replies()

    sixel_seq = b'\x1bP0;1;0q"1;1;16;32#0!16~-#0!16~-#0!16~-#0!16~-#0!16~\x1b\\'
    vt_sixel = libvt.Terminal(rows=6, cols=10, profile=Profile.tmux("next-3.9"))
    vt_sixel.feed(sixel_seq)
    assert (vt_sixel.placements[0].rows, vt_sixel.cursor.row) == (2, 2)
    vt_sixel.feed(b"\x1b[1;1HX")
    assert (
        vt_sixel.placements[0].occupies_abs(0, 0),
        vt_sixel.placements[0].occupies_abs(0, 1),
    ) == (False, True)
    with pytest.raises(TypeError):
        vt_sixel.placements[0].occupies_abs(True, 0)
    with pytest.raises(TypeError):
        vt_sixel.placements[0].occupies_abs(0, False)

    tm38_sixel = libvt.Terminal(rows=6, cols=10, profile=Profile.tmux("3.8a"))
    tm38_sixel.feed(sixel_seq + b"\x1b[1;1HX")
    assert len(tm38_sixel.placements) == 0

    # 10,000px SIXEL under DECLRMM stays O(rows) without per-row linefeed loops.
    dos_t = libvt.Terminal(rows=24, cols=80, record_events=True)
    t0 = time.monotonic()
    for _ in range(10):
        dos_t.feed(b'\x1b[?69h\x1b[10;20s\x1b[1;1H\x1bPq"1;1;10;10000#0~\x1b\\')
    assert time.monotonic() - t0 < 0.1

    ev_t = libvt.Terminal(rows=4, cols=8, record_events=True)
    raw_1cell = base64.b64encode(b"\xff" * (8 * 16 * 3))
    ev_t.feed(b"\x1b_Ga=T,f=24,s=8,v=16,i=5;" + raw_1cell + b"\x1b\\")
    evs = [e for e in ev_t.drain_events() if isinstance(e, KittyGraphic)]
    with pytest.raises(TypeError):
        t.cast("dict[str, str]", evs[0].command.control)["a"] = "d"
    with pytest.raises(TypeError):
        t.cast("dict[int, object]", ev_t.snapshot().images)[999] = None
