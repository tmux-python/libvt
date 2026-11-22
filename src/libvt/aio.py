"""Transport-only ``asyncio`` stream and POSIX PTY adapters over :class:`Terminal`."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import errno
import os
import signal
import socket
import struct
import sys
import time
import typing as t

from libvt.config import (
    DEFAULT_LIMITS,
    DEFAULT_OSC7_HOSTS,
    DEFAULT_POLICY,
    DEFAULT_PROFILE,
    Limits,
    Policy,
    Profile,
)
from libvt.exc import TerminalTimeoutError
from libvt.input import InputEvent, KeyEvent, PasteEvent
from libvt.terminal import FlightRecorder, Snapshot, Terminal

type TtyMode = t.Literal["cooked", "cbreak", "raw"]

_PTY_TRAMPOLINE = (
    "stage = 'bootstrap'\n"
    "err_fd = -1\n"
    "try:\n"
    "    import errno, fcntl, os, signal, sys, termios\n"
    "    err_fd = int(sys.argv[1])\n"
    "    os.set_inheritable(err_fd, False)\n"
    "    stage = 'ctty'\n"
    "    if hasattr(termios, 'TIOCSCTTY'):\n"
    "        fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "    stage = 'signal'\n"
    "    signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "    if hasattr(signal, 'SIGXFSZ'):\n"
    "        signal.signal(signal.SIGXFSZ, signal.SIG_DFL)\n"
    "    stage = 'exec'\n"
    "    os.write(err_fd, b'\\x00')\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except BaseException as exc:\n"
    "    try:\n"
    "        import errno as _e, os as _os\n"
    "        if err_fd >= 0:\n"
    "            code = getattr(exc, 'errno', None)\n"
    "            if not isinstance(code, int):\n"
    "                code = _e.ENOEXEC\n"
    "            msg = (getattr(exc, 'strerror', None) or str(exc))[:360]\n"
    "            pkt = b'\\x01' + f'{stage}:{code}:{msg}'.encode('utf-8', 'replace')\n"
    "            _os.write(err_fd, pkt[:500])\n"
    "    except BaseException:\n"
    "        pass\n"
    "    import os as _os2\n"
    "    _os2._exit(127)\n"
)


def _raise_exec_error(err_payload: bytes, cmd: str, default_errno: int) -> t.NoReturn:
    if not err_payload:
        msg = f"PTY launcher exited before pre-exec handoff while starting {cmd!r}"
        raise RuntimeError(msg)
    idx = err_payload.find(b"\x01")
    body = err_payload[idx + 1 :] if idx != -1 else err_payload
    parts = body.decode("utf-8", "replace").split(":", 2)
    if len(parts) == 3:
        stage, code_s, err_msg = parts
    else:
        stage, code_s, err_msg = "exec", "", body.decode("utf-8", "replace")
    err_no = int(code_s) if code_s.isdigit() else default_errno
    target = cmd if stage == "exec" else f"open_pty({stage})"
    raise OSError(err_no, err_msg or os.strerror(err_no), target)


async def _read_cloexec_pipe(loop: asyncio.AbstractEventLoop, fd: int) -> bytes:
    """Read non-blocking *fd* until EOF without spawning executor threads."""
    chunks: list[bytes] = []
    done = loop.create_future()

    def _on_ready() -> None:
        while True:
            try:
                data = os.read(fd, 4_096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                loop.remove_reader(fd)
                if not done.done():
                    done.set_exception(exc)
                return
            if not data:
                loop.remove_reader(fd)
                if not done.done():
                    done.set_result(b"".join(chunks))
                return
            chunks.append(data)

    loop.add_reader(fd, _on_ready)
    try:
        return await done
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(fd)


class ReplyWriter(t.Protocol):
    """Structural writer protocol satisfied by :class:`asyncio.StreamWriter`."""

    def write(self, data: bytes, /) -> object:
        """Buffer outbound *data* bytes."""

    async def drain(self) -> None:
        """Flush buffered outbound bytes."""

    def close(self) -> object:
        """Initiate writer close."""

    async def wait_closed(self) -> None:
        """Wait until writer close completes."""


class AsyncTerminalStream:
    """Drive a sans-I/O :class:`~libvt.terminal.Terminal` from ``asyncio`` streams.

    Coalesces frame wakeups across DEC mode ``?2026`` synchronized-output
    batches (bounded by *sync_timeout* using generation-safe expiry) and
    automatically flushes :meth:`~libvt.terminal.Terminal.drain_replies` when a
    ``StreamWriter`` is attached.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: ReplyWriter | None = None,
        *,
        terminal: Terminal | None = None,
        rows: int = 24,
        cols: int = 80,
        chunk_size: int = 4_096,
        sync_timeout: float = 1.0,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.terminal = (
            terminal if terminal is not None else Terminal(rows=rows, cols=cols)
        )
        self.chunk_size = chunk_size
        self.sync_timeout = sync_timeout
        self._frame_event = asyncio.Event()
        self._eof = False
        self._sync_started_at: float | None = None
        self._sync_gen_tracked: int | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once ``reader`` reaches end-of-stream."""
        return self._eof

    def _update_sync_timer(self, now: float) -> None:
        if self.terminal.synchronized:
            cur_gen = self.terminal.sync_generation
            if self._sync_gen_tracked != cur_gen or self._sync_started_at is None:
                self._sync_gen_tracked = cur_gen
                self._sync_started_at = now
            elif (
                now - self._sync_started_at >= self.sync_timeout
                and self.terminal.expire_synchronized_output(cur_gen)
            ):
                self._sync_started_at = None
                self._sync_gen_tracked = None
                self._frame_event.set()
        else:
            self._sync_started_at = None
            self._sync_gen_tracked = None
            self._frame_event.set()

    async def pump_once(self) -> bool:
        """Read and feed one chunk; return ``False`` on EOF."""
        chunk = await self.reader.read(self.chunk_size)
        if not chunk:
            self._eof = True
            self._frame_event.set()
            return False
        self.terminal.feed(chunk)
        replies = self.terminal.drain_replies()
        if replies and self.writer is not None:
            self.writer.write(replies)
            await self.writer.drain()
        self._update_sync_timer(time.monotonic())
        return True

    async def _pump_loop(self) -> None:
        while await self.pump_once():
            pass

    def start(self) -> asyncio.Task[None]:
        """Start background stream pumping on the running event loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump_loop())
        return self._task

    async def aclose(self) -> None:
        """Cancel background pump task and close the writer if present."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()

    async def __aenter__(self) -> t.Self:
        """Start background pumping when entering an ``async with`` block."""
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """Stop background pumping and close writer when leaving ``async with``."""
        await self.aclose()

    async def wait_for(
        self,
        predicate: collections.abc.Callable[[Snapshot], bool] | str,
        *,
        timeout: float = 2.0,
    ) -> Snapshot:
        """Wait until *predicate* matches the committed :class:`Snapshot`."""
        check = (
            (lambda snap: predicate in snap.text)
            if isinstance(predicate, str)
            else predicate
        )
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if self.terminal.synchronized:
                self._update_sync_timer(now)
            snap = self.terminal.snapshot() if self._eof else self.terminal.committed
            if check(snap):
                return snap
            if self._eof:
                msg = (
                    f"stream reached EOF before predicate matched; "
                    f"screen text={snap.text!r}"
                )
                raise TerminalTimeoutError(msg)
            remaining = deadline - now
            if remaining <= 0:
                live = self.terminal.snapshot()
                if check(live):
                    return live
                msg = (
                    f"timed out after {timeout:.2f}s waiting for terminal state; "
                    f"screen text={live.text!r}"
                )
                raise TerminalTimeoutError(msg)
            self._frame_event.clear()
            wait_slice = (
                min(remaining, self.sync_timeout)
                if self.terminal.synchronized
                else remaining
            )
            if self._task is None:
                try:
                    await asyncio.wait_for(self.pump_once(), timeout=wait_slice)
                except TimeoutError as exc:
                    if self.terminal.synchronized:
                        self._update_sync_timer(time.monotonic())
                        continue
                    msg = f"timed out after {timeout:.2f}s waiting for stream bytes"
                    raise TerminalTimeoutError(msg) from exc
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._frame_event.wait(),
                        timeout=wait_slice,
                    )
                if self.terminal.synchronized:
                    self._update_sync_timer(time.monotonic())


class PtySession:
    """Async POSIX pseudo-terminal subprocess session backed by :class:`Terminal`."""

    def __init__(
        self,
        *,
        master_fd: int,
        process: asyncio.subprocess.Process,
        terminal: Terminal,
        sync_timeout: float = 1.0,
    ) -> None:
        self._master_fd = master_fd
        self.process = process
        self.terminal = terminal
        self.sync_timeout = sync_timeout
        self._frame_event = asyncio.Event()
        self._eof = False
        self._closed = False
        self._sync_started_at: float | None = None
        self._sync_gen_tracked: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def eof(self) -> bool:
        """Return ``True`` once the PTY master reaches EOF."""
        return self._eof

    def _drain_nonblocking_master(self) -> None:
        """Consume any buffered non-blocking bytes from ``master_fd``."""
        if self._closed or self._eof:
            return
        while True:
            try:
                chunk = os.read(self._master_fd, 16_384)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno == errno.EIO:
                    chunk = b""
                else:
                    return
            if not chunk:
                self._eof = True
                return
            self.terminal.feed(chunk)

    def _on_readable(self) -> None:
        if self._closed:
            return
        try:
            chunk = os.read(self._master_fd, 16_384)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno == errno.EIO:
                chunk = b""
            else:
                raise
        if not chunk:
            self._eof = True
            if self._loop is not None:
                self._loop.remove_reader(self._master_fd)
            self._frame_event.set()
            return
        self.terminal.feed(chunk)
        replies = self.terminal.drain_replies()
        if replies:
            with contextlib.suppress(OSError):
                os.write(self._master_fd, replies)
        now = time.monotonic()
        if self.terminal.synchronized:
            cur_gen = self.terminal.sync_generation
            if self._sync_gen_tracked != cur_gen or self._sync_started_at is None:
                self._sync_gen_tracked = cur_gen
                self._sync_started_at = now
        else:
            self._sync_started_at = None
            self._sync_gen_tracked = None
            self._frame_event.set()

    def send(self, data: bytes | str, /) -> None:
        """Write raw *data* bytes (or UTF-8 string) to the PTY master."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        os.write(self._master_fd, raw)

    def send_event(self, event: InputEvent, /) -> None:
        """Encode *event* via :meth:`Terminal.encode` and write to the PTY."""
        encoded = self.terminal.encode(event)
        if encoded:
            os.write(self._master_fd, encoded)

    def send_key(self, chord: str, /) -> None:
        """Parse *chord* (e.g. ``"ctrl+c"``, ``"enter"``) and send to the PTY."""
        self.send_event(KeyEvent.parse(chord))

    def paste(self, text: str, /, *, sanitize: bool = False) -> None:
        """Encode *text* as a :class:`PasteEvent` and write to the PTY."""
        self.send_event(PasteEvent(text=text, sanitize=sanitize))

    def resize(
        self,
        *,
        rows: int,
        cols: int,
        reflow: bool | None = None,
    ) -> None:
        """Update kernel ``TIOCSWINSZ`` and resize :attr:`terminal`."""
        import fcntl
        import termios

        winsz = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsz)
        self.terminal.resize(rows=rows, cols=cols, reflow=reflow)

    async def wait_for(
        self,
        predicate: collections.abc.Callable[[Snapshot], bool] | str,
        *,
        timeout: float = 2.0,
    ) -> Snapshot:
        """Wait until *predicate* matches the committed :class:`Snapshot`."""
        check = (
            (lambda snap: predicate in snap.text)
            if isinstance(predicate, str)
            else predicate
        )
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            if (
                self.terminal.synchronized
                and self._sync_started_at is not None
                and now - self._sync_started_at >= self.sync_timeout
            ):
                self.terminal.expire_synchronized_output(self._sync_gen_tracked)
                self._sync_started_at = None
                self._sync_gen_tracked = None
            snap = self.terminal.snapshot() if self._eof else self.terminal.committed
            if check(snap):
                return snap
            if self._eof:
                msg = (
                    f"PTY reached EOF before predicate matched; "
                    f"screen text={snap.text!r}"
                )
                raise TerminalTimeoutError(msg)
            remaining = deadline - now
            if remaining <= 0:
                live = self.terminal.snapshot()
                if check(live):
                    return live
                msg = (
                    f"timed out after {timeout:.2f}s waiting for PTY state; "
                    f"screen text={live.text!r}"
                )
                raise TerminalTimeoutError(msg)
            self._frame_event.clear()
            wait_slice = (
                min(remaining, self.sync_timeout)
                if self.terminal.synchronized
                else remaining
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._frame_event.wait(), timeout=wait_slice)

    async def aclose(self) -> int:
        """Signal the owned process group, drain PTY output, and close master fd."""
        if self.process.returncode is None:
            pid = self.process.pid
            for sig, wait_s in (
                (signal.SIGHUP, 0.4),
                (signal.SIGTERM, 0.4),
            ):
                if self.process.returncode is not None:
                    break
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, sig)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.process.wait(), timeout=wait_s)
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()

        if not self._closed:
            self._drain_nonblocking_master()
            self._closed = True
            if self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.remove_reader(self._master_fd)
            with contextlib.suppress(OSError):
                os.close(self._master_fd)

        self.terminal.notify_process_exit()
        return self.process.returncode or 0


@contextlib.asynccontextmanager
async def open_pty(
    argv: collections.abc.Sequence[str],
    *,
    rows: int = 24,
    cols: int = 80,
    scrollback: int = 2_000,
    env: collections.abc.Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    tty_mode: TtyMode = "cooked",
    python_executable: str | os.PathLike[str] | None = None,
    profile: Profile = DEFAULT_PROFILE,
    policy: Policy = DEFAULT_POLICY,
    limits: Limits = DEFAULT_LIMITS,
    sync_timeout: float = 1.0,
) -> collections.abc.AsyncGenerator[PtySession]:
    """Spawn *argv* with a controlling POSIX PTY driven by :class:`Terminal`."""
    if sys.platform == "win32":
        msg = "open_pty requires POSIX pty.openpty()"
        raise NotImplementedError(msg)
    if not argv:
        msg = "argv must not be empty"
        raise ValueError(msg)
    if tty_mode not in ("cooked", "cbreak", "raw"):
        msg = f"unsupported tty_mode: {tty_mode!r}"
        raise ValueError(msg)

    if python_executable is None:
        if getattr(sys, "frozen", False) or not sys.executable:
            msg = (
                "open_pty requires python_executable in frozen or embedded "
                "Python runtimes"
            )
            raise RuntimeError(msg)
        py_bin = sys.executable
    else:
        py_bin = os.fspath(python_executable)

    import fcntl
    import pty
    import termios
    import tty

    loop = asyncio.get_running_loop()
    master_fd, slave_fd = pty.openpty()
    err_r, err_w = os.pipe()
    slave_closed = False
    err_w_closed = False
    proc: asyncio.subprocess.Process | None = None
    try:
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        if tty_mode == "cbreak":
            tty.setcbreak(slave_fd)
        elif tty_mode == "raw":
            tty.setraw(slave_fd)
        os.set_blocking(master_fd, False)
        os.set_blocking(err_r, False)
        child_env = dict(os.environ if env is None else env)
        child_env.setdefault("TERM", "xterm-256color")
        proc = await asyncio.create_subprocess_exec(
            py_bin,
            "-I",
            "-S",
            "-c",
            _PTY_TRAMPOLINE,
            str(err_w),
            *argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=child_env,
            start_new_session=True,
            pass_fds=(err_w,),
            close_fds=True,
        )
        os.close(slave_fd)
        slave_closed = True
        os.close(err_w)
        err_w_closed = True

        err_payload = await _read_cloexec_pipe(loop, err_r)
        if err_payload != b"\x00":
            await proc.wait()
            _raise_exec_error(err_payload, argv[0], errno.ENOEXEC)
    except BaseException:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        with contextlib.suppress(OSError):
            os.close(master_fd)
        raise
    finally:
        if not slave_closed:
            with contextlib.suppress(OSError):
                os.close(slave_fd)
        if not err_w_closed:
            with contextlib.suppress(OSError):
                os.close(err_w)
        with contextlib.suppress(OSError):
            os.close(err_r)

    eff_policy = policy
    if policy.osc7_hosts is None:
        fqdn = socket.gethostname().lower()
        short = fqdn.partition(".")[0]
        eff_policy = dataclasses.replace(
            policy,
            osc7_hosts=DEFAULT_OSC7_HOSTS | frozenset({fqdn, short}),
        )

    recorder = FlightRecorder(max_bytes=limits.max_recorder_bytes)
    terminal = Terminal(
        rows=rows,
        cols=cols,
        scrollback=scrollback,
        profile=profile,
        policy=eff_policy,
        limits=limits,
        recorder=recorder,
    )
    session = PtySession(
        master_fd=master_fd,
        process=proc,
        terminal=terminal,
        sync_timeout=sync_timeout,
    )
    session._loop = loop
    loop.add_reader(master_fd, session._on_readable)
    try:
        yield session
    finally:
        await session.aclose()


__all__ = [
    "AsyncTerminalStream",
    "PtySession",
    "ReplyWriter",
    "TtyMode",
    "open_pty",
]
