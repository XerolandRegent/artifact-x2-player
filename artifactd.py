"""
ARTIFACT - artifactd v0.1
File: artifactd.py
Purpose: bridge between the ARTIFACT web UI and MPD. Serves the static UI,
         exposes a WebSocket at /ws carrying the control vocabulary
         (play/pause, stop, relative seek, volume, track selection) and
         broadcasts player state to all connected clients.
Run:     ~/artifact/venv/bin/python artifactd.py   (listens on 0.0.0.0:8080)
Date:    2026-07-18
"""

import asyncio
import json
import logging
import os
import re
import signal
import socket
from contextlib import asynccontextmanager

import mutagen
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from mpd.asyncio import MPDClient

MPD_HOST = os.environ.get("ARTIFACT_MPD_HOST", "localhost")
MPD_PORT = int(os.environ.get("ARTIFACT_MPD_PORT", "6600"))
HTTP_PORT = int(os.environ.get("ARTIFACT_HTTP_PORT", "8080"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MUSIC_DIR = os.environ.get("ARTIFACT_MUSIC_DIR",
                           os.path.expanduser("~/music"))
ALLOWED_EXT = {".flac", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".aiff", ".wv"}

# Deck mode: the xwax-derived scratch engine
DECK_DIR = os.environ.get("ARTIFACT_DECK_DIR",
                          os.path.expanduser("~/ScratchTJ/software"))
DECK_BIN = os.path.join(DECK_DIR, "xwax")
REEL_SOCK = "/tmp/artifact-reel.sock"
DECK_LOG = "/tmp/artifact-deck.log"

log = logging.getLogger("artifactd")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


class DeckManager:
    """Owns deck mode: hands the DAC from MPD to the scratch engine,
    streams reel angle into it, and hands the device back on exit."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.sock: socket.socket | None = None
        self.track_name = ""
        self.error = ""

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def state(self) -> dict:
        return {
            "active": self.active,
            "track": self.track_name,
            "error": self.error,
        }

    @staticmethod
    async def _systemctl(action: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/systemctl", action, "mpd.socket", "mpd",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            log.warning("systemctl %s mpd failed (rc=%s) — sudoers rule missing?",
                        action, proc.returncode)
        return proc.returncode == 0

    async def enter(self, path: str, title: str) -> None:
        if self.active:
            await self.exit()
        if not os.path.isfile(DECK_BIN):
            self.error = "engine missing"
            log.warning("deck engine not found at %s", DECK_BIN)
            return
        self.error = ""
        self.track_name = title

        await self._systemctl("stop")
        await asyncio.sleep(0.3)  # let ALSA release the device

        try:
            os.unlink(REEL_SOCK)
        except OSError:
            pass

        logfile = open(DECK_LOG, "wb")
        self.proc = await asyncio.create_subprocess_exec(
            DECK_BIN, path, cwd=DECK_DIR, stdout=logfile, stderr=logfile,
        )
        log.info("deck engine started (pid %s) with %s", self.proc.pid, path)

        for _ in range(100):  # up to 10s for the engine to bind its socket
            if os.path.exists(REEL_SOCK):
                break
            if self.proc.returncode is not None:
                self.error = "engine exited"
                log.warning("deck engine died on startup; see %s", DECK_LOG)
                await self._systemctl("start")
                return
            await asyncio.sleep(0.1)

        if not os.path.exists(REEL_SOCK):
            self.error = "no reel socket"
            log.warning("deck engine never created %s", REEL_SOCK)
            return

        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
            self.sock.connect(REEL_SOCK)
            log.info("reel socket connected")
        except OSError as exc:
            self.error = "socket failed"
            log.warning("reel socket connect failed: %s", exc)

    def reel(self, angle: float, touch: int) -> None:
        if self.sock is None:
            return
        try:
            self.sock.send(f"A {angle:.2f} {1 if touch else 0}".encode())
        except OSError:
            pass

    def send_raw(self, msg: str) -> None:
        if self.sock is None:
            return
        try:
            self.sock.send(msg.encode())
        except OSError:
            pass

    async def exit(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.proc is not None and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
            log.info("deck engine stopped")
        self.proc = None
        self.track_name = ""
        await asyncio.sleep(0.3)
        await self._systemctl("start")


class MpdBridge:
    """Owns the MPD connection, state snapshots, and client broadcasts."""

    def __init__(self) -> None:
        self.client = MPDClient()
        self.connected = False
        self.sockets: set[WebSocket] = set()
        self.held_was_playing = False
        self._lock = asyncio.Lock()

    # ---------- connection ----------

    async def connect_forever(self) -> None:
        while True:
            try:
                await self.client.connect(MPD_HOST, MPD_PORT)
                self.connected = True
                log.info("connected to MPD at %s:%s", MPD_HOST, MPD_PORT)
                await self.broadcast_state()
                async for _subsystems in self.client.idle():
                    await self.broadcast_state()
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self.connected = False
                if not deck.active:
                    log.warning("MPD connection lost (%s); retrying in 2s", exc)
                try:
                    self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self.client = MPDClient()
                await self.broadcast_state()
                await asyncio.sleep(2)

    async def ticker(self) -> None:
        """Position updates at 4 Hz while playing, so counters stay honest."""
        while True:
            await asyncio.sleep(0.25)
            if not self.connected or not self.sockets:
                continue
            try:
                status = await self.client.status()
            except Exception:  # noqa: BLE001
                continue
            if status.get("state") == "play":
                msg = json.dumps({
                    "type": "tick",
                    "elapsed": float(status.get("elapsed", 0.0)),
                })
                await self._send_all(msg)

    # ---------- state ----------

    async def snapshot(self) -> dict:
        if not self.connected:
            return {"type": "state", "connected": False, "deck": deck.state()}
        status = await self.client.status()
        song = await self.client.currentsong()
        queue = await self.client.playlistinfo()
        playlist = [
            {
                "pos": int(item.get("pos", i)),
                "title": item.get("title") or os.path.splitext(os.path.basename(item.get("file", "?")))[0],
                "artist": item.get("artist", ""),
                "duration": float(item.get("duration", 0.0) or 0.0),
                "file": item.get("file", ""),
            }
            for i, item in enumerate(queue)
        ]
        return {
            "type": "state",
            "connected": True,
            "playing": status.get("state") == "play",
            "state": status.get("state", "stop"),
            "elapsed": float(status.get("elapsed", 0.0) or 0.0),
            "duration": float(status.get("duration", 0.0) or 0.0),
            "volume": int(status.get("volume", -1) or -1),
            "repeat": status.get("repeat") == "1",
            "single": status.get("single", "0") in ("1", "oneshot"),
            "error": status.get("error", ""),
            "audio": status.get("audio", ""),
            "songpos": int(song.get("pos", -1)) if song.get("pos") is not None else -1,
            "title": song.get("title")
                     or os.path.splitext(os.path.basename(song.get("file", "")))[0]
                     or "no track",
            "artist": song.get("artist", "local library"),
            "file": song.get("file", ""),
            "playlist": playlist,
            "deck": deck.state(),
        }

    async def broadcast_state(self) -> None:
        try:
            state = await self.snapshot()
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot failed: %s", exc)
            return
        await self._send_all(json.dumps(state))

    async def _send_all(self, message: str) -> None:
        dead = []
        for ws in self.sockets:
            try:
                await ws.send_text(message)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    # ---------- commands ----------

    async def handle(self, msg: dict) -> None:
        if not self.connected:
            return
        cmd = msg.get("cmd", "")
        async with self._lock:
            try:
                if cmd == "play_toggle":
                    status = await self.client.status()
                    if status.get("error"):
                        await self.client.clearerror()
                    if status.get("state") == "play":
                        await self.client.pause(1)
                    elif status.get("state") == "pause":
                        await self.client.pause(0)
                    else:
                        await self.client.play()
                elif cmd == "move":
                    await self.client.move(int(msg.get("from", 0)), int(msg.get("to", 0)))
                elif cmd == "remove":
                    await self.client.delete(int(msg.get("i", 0)))
                elif cmd == "delete_file":
                    path = await self._queue_path(int(msg.get("i", -1)))
                    if path:
                        await self.client.delete(int(msg.get("i", 0)))
                        try:
                            os.remove(path)
                            log.info("deleted file %s", path)
                        except OSError as exc:
                            log.warning("delete failed for %s: %s", path, exc)
                        await self.client.update()
                elif cmd == "set_tags":
                    path = await self._queue_path(int(msg.get("i", -1)))
                    if path:
                        title = str(msg.get("title", "")).strip()
                        artist = str(msg.get("artist", "")).strip()
                        ok = await asyncio.to_thread(self._write_tags, path, title, artist)
                        if ok:
                            await self.client.update()
                elif cmd == "clear_error":
                    await self.client.clearerror()
                elif cmd == "deck_enter":
                    i = int(msg.get("i", -1))
                    path = await self._queue_path(i)
                    if path:
                        queue = await self.client.playlistinfo()
                        title = (queue[i].get("title")
                                 or os.path.splitext(os.path.basename(path))[0]) if i < len(queue) else ""
                        asyncio.create_task(deck.enter(path, title))
                    else:
                        log.warning("deck_enter: no file at queue index %s", i)
                elif cmd == "deck_exit":
                    asyncio.create_task(deck.exit())
                elif cmd == "stop":
                    await self.client.stop()
                elif cmd == "next":
                    await self.client.next()
                elif cmd == "prev":
                    await self.client.previous()
                elif cmd == "seek_by":
                    delta = float(msg.get("d", 0.0))
                    if delta != 0.0:
                        sign = "+" if delta >= 0 else "-"
                        await self.client.seekcur(f"{sign}{abs(delta):.3f}")
                elif cmd == "vol_delta":
                    status = await self.client.status()
                    vol = int(status.get("volume", 0) or 0)
                    vol = max(0, min(100, vol + int(msg.get("d", 0))))
                    await self.client.setvol(vol)
                elif cmd == "vol_set":
                    await self.client.setvol(max(0, min(100, int(msg.get("v", 0)))))
                elif cmd == "play_index":
                    await self.client.play(int(msg.get("i", 0)))
                elif cmd == "playmode":
                    m = msg.get("m", "all")  # all | one | loopall | loopone
                    await self.client.single(1 if m in ("one", "loopone") else 0)
                    await self.client.repeat(1 if m in ("loopall", "loopone") else 0)
                elif cmd == "hold":
                    # The TP-7 gesture: finger on the reel pauses; lifting resumes.
                    if msg.get("on"):
                        status = await self.client.status()
                        self.held_was_playing = status.get("state") == "play"
                        if self.held_was_playing:
                            await self.client.pause(1)
                    else:
                        if self.held_was_playing:
                            await self.client.pause(0)
                        self.held_was_playing = False
                elif cmd == "poweroff":
                    log.info("poweroff requested from UI")
                    proc = await asyncio.create_subprocess_exec(
                        "sudo", "-n", "/usr/bin/systemctl", "poweroff"
                    )
                    await proc.wait()
                    if proc.returncode != 0:
                        log.warning("poweroff failed (rc=%s) — is the sudoers rule installed?", proc.returncode)
                elif cmd == "refresh":
                    pass  # state broadcast below covers it
                else:
                    log.info("unknown cmd: %s", cmd)
            except Exception as exc:  # noqa: BLE001
                log.warning("cmd %s failed: %s", cmd, exc)
        await self.broadcast_state()


    async def _queue_path(self, index: int) -> str | None:
        """Absolute file path for a queue index, confined to MUSIC_DIR."""
        if index < 0:
            return None
        try:
            queue = await self.client.playlistinfo()
        except Exception:  # noqa: BLE001
            return None
        if index >= len(queue):
            return None
        rel = queue[index].get("file", "")
        path = os.path.realpath(os.path.join(MUSIC_DIR, rel))
        root = os.path.realpath(MUSIC_DIR)
        if not path.startswith(root + os.sep) and path != root:
            log.warning("path escape blocked: %s", rel)
            return None
        return path if os.path.isfile(path) else None

    @staticmethod
    def _write_tags(path: str, title: str, artist: str) -> bool:
        try:
            audio = mutagen.File(path, easy=True)
            if audio is None:
                return False
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
            audio.save()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("tag write failed for %s: %s", path, exc)
            return False

    async def ingest(self, names: list[str]) -> None:
        """After files land in MUSIC_DIR: rescan the MPD database, wait for
        the scan to finish, then append the new files to the queue."""
        if not self.connected:
            return
        async with self._lock:
            try:
                await self.client.update()
                for _ in range(100):  # up to ~20s for large uploads
                    status = await self.client.status()
                    if "updating_db" not in status:
                        break
                    await asyncio.sleep(0.2)
                for name in names:
                    try:
                        await self.client.add(name)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("could not queue %s: %s", name, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("ingest failed: %s", exc)
        await self.broadcast_state()


deck = DeckManager()
bridge = MpdBridge()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(bridge.connect_forever()),
        asyncio.create_task(bridge.ticker()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    bridge.sockets.add(ws)
    log.info("client connected (%d total)", len(bridge.sockets))
    try:
        await ws.send_text(json.dumps(await bridge.snapshot()))
    except Exception:  # noqa: BLE001
        pass
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cmd = msg.get("cmd", "")
            if cmd == "reel":
                # 60 Hz stream: straight to the engine, no locking or broadcast
                deck.reel(float(msg.get("a", 0.0)), int(msg.get("t", 0)))
                continue
            if cmd == "deck_pitch":
                deck.send_raw(f"P {float(msg.get('p', 1.0)):.4f}")
                continue
            if cmd == "deck_stop":
                deck.send_raw(f"S {1 if msg.get('on') else 0}")
                continue
            if cmd == "deck_exit":
                # must work while MPD is down — that is the whole point
                await deck.exit()
                await asyncio.sleep(0.8)
                await bridge.broadcast_state()
                continue
            await bridge.handle(msg)
            if cmd == "deck_enter":
                await asyncio.sleep(1.2)
                await bridge.broadcast_state()
    except WebSocketDisconnect:
        pass
    finally:
        bridge.sockets.discard(ws)
        log.info("client disconnected (%d total)", len(bridge.sockets))


@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    os.makedirs(MUSIC_DIR, exist_ok=True)
    saved: list[str] = []
    skipped: list[str] = []
    for f in files:
        name = os.path.basename(f.filename or "")
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in ALLOWED_EXT:
            skipped.append(name or "unnamed")
            continue
        name = re.sub(r"[^\w\-. ()\[\]&']", "_", name)
        dest = os.path.join(MUSIC_DIR, name)
        data = await f.read()
        with open(dest, "wb") as out:
            out.write(data)
        saved.append(name)
        log.info("uploaded %s (%d bytes)", name, len(data))
    if saved:
        await bridge.ingest(saved)
    return {"saved": saved, "skipped": skipped}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")
