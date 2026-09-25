#!/usr/bin/env python3
# -*- coding: utf-8 -*-

##~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
## Copyright (c) 2008, Martin W. Kirst All rights reserved.
##
## Redistribution and use in source and binary forms, with or without
## modification, are permitted provided that the following conditions are
## met:
##
## Redistributions of source code must retain the above copyright notice,
## this list of conditions and the following disclaimer.
## Redistributions in binary form must reproduce the above copyright notice,
## this list of conditions and the following disclaimer in the documentation
## and/or other materials provided with the distribution.
##
## THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
## IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
## TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
## PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
## HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
## SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
## TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
## PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
## LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
## NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
## SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
##~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

"""
  ASCII art movie Telnet player.
  Version         : 0.3  (HTML stats, reverse DNS, IP auto-ban)

  Can stream an ~20 minutes ASCII movie via Telnet emulation
  as stand alone server or via xinetd daemon.
  Tested with Python 2.3, Python 2.5, Python 2.7
  Ported to Python 3.14.6

  Original art work : Simon Jansen ( http://www.asciimation.co.nz/ )
  Telnetification
  & Player coding   : Martin W. Kirst ( https://github.com/nitram509/ascii-telnet-server )
"""

import sys
import time
import json
import socket
import queue
import urllib.parse
import http.server
import socketserver
import threading
import os.path
from io import StringIO
import argparse


###############################################################################
MAXDIM = (80, 24)   # maximum dimension of the VT100 terminal
MAX_CLIENTS = 20    # maximum number of simultaneous client connections

_START_TIME = time.time()


###############################################################################
class VT100Codes:
    """
        Some escape codes used within VT100 Streams
        @see: http://ascii-table.com/ansi-escape-sequences-vt-100.php
    """
    ESC = chr(27)          # VT100 escape character constant
    JMPHOME = ESC + "[H"   # Move cursor to upper left corner
    CLEARSCRN = ESC + "[2J"  # Clear entire screen
    CLEARDOWN = ESC + "[J"   # Clear screen from cursor down

    def JMPXY(self, intX, intY):
        """
            Send VT100 commands: goto position X,Y
            @param intX: x coordinate (starting at 1)
            @param intY: y coordinate (starting at 1)
            @return: the VT100 code as a string
        """
        if not (intX < 0 or intX > MAXDIM[0] or intY < 0 or intY > MAXDIM[1]):
            return VT100Codes.ESC + "[" + str(intY) + ";" + str(intX) + "H"
        else:
            sys.stderr.write("Warning, coordinates out of range. (%d,%d)\n" % (intX, intY))
            return ""


###############################################################################
class Frame:
    """
        One frame is 67 columns and 13 rows in effective size on screen.
        Use 'displayTime' to get the frame cycles this specific frame should
        be displayed.
        Use 'data' to get the 13 lines.
    """
    def __init__(self):
        self.displayTime = 1  # number of frame cycles to display this frame, default 1
        self.data = []        # 13 lines


###############################################################################
class Movie:
    """
        A movie consists of frames and is empty by default.
        Movies are loaded from text files.
        Use 'dimension' to get the dimension of the movie.
        A movie only can be loaded once. A second try will fail.
    """

    def __init__(self):
        self.__frames = []
        self.__loaded = False
        self.dimension = (67, 13)
        f = Frame()
        f.data.append("No movie yet loaded.")
        self.__frames.append(f)

    def loadMovie(self, fileName):
        """
            Loads the ASCII movie from given text file.
            Using an encoded format, described on
            http://www.asciimation.co.nz/asciimation/ascii_faq.html
            In short:
            67x14 chars,
            lines separated with 0x0a,
            first line is a number telling delay in number of frames,
            13 lines effective movie size,
            15 frames per second,
        """
        if self.__loaded:
            # we don't want to be loaded twice.
            return False

        with open(fileName, "r", encoding="iso-8859-15") as f:
            self.__frames = []
            currentFrame = None
            counter = 0
            maxLinesPerFrame = self.dimension[1] + 1   # incl. meta data (time information)
            maxWidth = self.dimension[0]

            for l in f.readlines():
                i = -1
                if (counter % maxLinesPerFrame) == 0:
                    try:
                        i = int(l[0:3])
                    except ValueError:
                        i = -1

                if (len(l.strip()) <= 3) and (i > 0) and (i <= 999):
                    currentFrame = Frame()
                    currentFrame.data = []
                    currentFrame.displayTime = i
                    self.__frames.append(currentFrame)
                else:
                    if currentFrame:
                        # first strip every white character from the right
                        l = l.rstrip()
                        # second fill them with blanks, that they later
                        # automatically clear old lines from screen
                        l = l.ljust(maxWidth)
                        # to center the frame on the screen, we also add some
                        # BLANKs on the left side
                        l = l.rjust(maxWidth + (MAXDIM[0] - maxWidth) // 2)
                        currentFrame.data.append(l)

                counter += 1

        self.__loaded = True
        return True

    def getEncFrames(self):
        """
            return a list with frames.
            Each frame carries its own display time, thats why it's 'encoded'.
        """
        return self.__frames


###############################################################################
#  In-memory statistics (clients)
###############################################################################
class ClientStats:
    """In-memory record about one connected/closed client."""

    __slots__ = ("client_id", "ip", "port", "start_time", "end_time",
                 "bytes_sent", "input_bytes", "frames_sent", "active")

    def __init__(self, client_id, ip, port):
        self.client_id = client_id
        self.ip = ip
        self.port = port
        self.start_time = time.time()
        self.end_time = None
        self.bytes_sent = 0
        self.input_bytes = 0
        self.frames_sent = 0
        self.active = True


class StatsRegistry:
    """
        Thread-safe, in-memory statistics registry.
        Nothing is written to disk: everything is lost when the process exits.
    """

    _lock = threading.Lock()
    _clients = {}                 # client_id -> ClientStats
    _next_id = 1
    _total_connections = 0
    _total_bytes = 0

    @classmethod
    def register(cls, ip, port):
        with cls._lock:
            cid = cls._next_id
            cls._next_id += 1
            st = ClientStats(cid, ip, port)
            cls._clients[cid] = st
            cls._total_connections += 1
            return st

    @classmethod
    def unregister(cls, stats):
        if stats is None:
            return
        with cls._lock:
            stats.active = False
            stats.end_time = time.time()

    @classmethod
    def add_bytes(cls, stats, nbytes):
        if stats is None or nbytes <= 0:
            return
        with cls._lock:
            stats.bytes_sent += nbytes
            stats.frames_sent += 1
            cls._total_bytes += nbytes

    @classmethod
    def add_input(cls, stats, nbytes):
        if stats is None or nbytes <= 0:
            return
        with cls._lock:
            stats.input_bytes += nbytes

    @classmethod
    def snapshot(cls):
        with cls._lock:
            now = time.time()
            clients = []
            active_count = 0
            for c in cls._clients.values():
                if c.active:
                    active_count += 1
                    end = now
                else:
                    end = c.end_time if c.end_time is not None else now
                clients.append({
                    "id": c.client_id,
                    "ip": c.ip,
                    "port": c.port,
                    "active": c.active,
                    "bytes_sent": c.bytes_sent,
                    "input_bytes": c.input_bytes,
                    "frames_sent": c.frames_sent,
                    "duration": round(end - c.start_time, 2),
                    "connected_at": time.strftime("%H:%M:%S",
                                                  time.localtime(c.start_time)),
                })
            return {
                "clients": clients,
                "active": active_count,
                "max_clients": MAX_CLIENTS,
                "total_connections": cls._total_connections,
                "total_bytes": cls._total_bytes,
                "uptime": round(now - _START_TIME, 1),
                "server_time": time.strftime("%H:%M:%S"),
            }


###############################################################################
#  In-memory IP ban list
###############################################################################
class BanRegistry:
    """
        Heuristic, in-memory ban list.

        Rules (all configurable via class attributes):
          * Any *incoming* payload from the client is suspicious — this is a
            one-way streaming server, a healthy viewer sends nothing.
            A burst of >= BAN_BYTES_THRESHOLD bytes within BAN_BYTES_WINDOW
            seconds triggers a ban.
          * Any occurrence of common auth-related keywords in the incoming
            payload (password, login, admin, ...) triggers an immediate ban.
            This is what catches "password brute-force" style probing against
            the Telnet endpoint.
          * More than BAN_CONN_THRESHOLD new TCP connections from the same IP
            within BAN_CONN_WINDOW seconds triggers a ban as well.

        Bans live in memory only and are gone when the process exits.
        Repeat offences extend the ban and increment the "hits" counter.
    """

    BAN_BYTES_THRESHOLD = 128       # bytes of input within window -> ban
    BAN_BYTES_WINDOW = 10.0         # seconds
    BAN_CONN_THRESHOLD = 10         # connections within window -> ban
    BAN_CONN_WINDOW = 30.0          # seconds
    BAN_DURATION = 600.0            # seconds to keep an IP banned

    # Lowercased keywords searched inside incoming payload
    SUSPICIOUS_PATTERNS = (
        b"password", b"passwd", b"pass:",
        b"login", b"log in", b"signin", b"sign in",
        b"admin", b"root:", b"root@",
        b"user:", b"username",
        b"user ", b"pass ",
        b"authorization:",
    )

    _lock = threading.Lock()
    _bans = {}       # ip -> {"until": float, "reason": str, "banned_at": float, "hits": int}
    _incoming = {}   # ip -> [(ts, nbytes), ...]
    _conns = {}      # ip -> [ts, ...]

    # ---- public queries -----------------------------------------------------
    @classmethod
    def get_ban(cls, ip):
        with cls._lock:
            b = cls._bans.get(ip)
            if not b:
                return None
            if b["until"] <= time.time():
                del cls._bans[ip]
                return None
            return dict(b)

    # ---- event hooks --------------------------------------------------------
    @classmethod
    def record_connection(cls, ip):
        """
            Called on every new TCP connection.
            @return: (banned_now, reason)
        """
        with cls._lock:
            b = cls._bans.get(ip)
            if b and b["until"] > time.time():
                return True, b["reason"]

            now = time.time()
            lst = [t for t in cls._conns.get(ip, []) if now - t <= cls.BAN_CONN_WINDOW]
            lst.append(now)
            cls._conns[ip] = lst
            if len(lst) >= cls.BAN_CONN_THRESHOLD:
                reason = "connection flood (%d in %.0fs)" % (len(lst), cls.BAN_CONN_WINDOW)
                cls._ban_locked(ip, reason)
                return True, reason
            return False, None

    @classmethod
    def record_incoming(cls, ip, data):
        """
            Called for every incoming chunk from the client.
            @return: (banned_now, reason)
        """
        with cls._lock:
            b = cls._bans.get(ip)
            if b and b["until"] > time.time():
                return True, b["reason"]

            # 1) keyword detection
            try:
                low = bytes(data).lower()
            except Exception:
                low = b""
            for pat in cls.SUSPICIOUS_PATTERNS:
                if pat in low:
                    reason = "suspicious input (%r)" % pat.decode("ascii", "replace")
                    cls._ban_locked(ip, reason)
                    return True, reason

            # 2) byte-flood detection
            now = time.time()
            nbytes = len(data)
            lst = [(t, n) for (t, n) in cls._incoming.get(ip, [])
                   if now - t <= cls.BAN_BYTES_WINDOW]
            lst.append((now, nbytes))
            cls._incoming[ip] = lst
            total = sum(n for _, n in lst)
            if total >= cls.BAN_BYTES_THRESHOLD:
                reason = "telnet input flood (%d bytes in %.0fs)" % (
                    total, cls.BAN_BYTES_WINDOW)
                cls._ban_locked(ip, reason)
                return True, reason
            return False, None

    @classmethod
    def unban(cls, ip):
        with cls._lock:
            cls._bans.pop(ip, None)
            cls._incoming.pop(ip, None)
            cls._conns.pop(ip, None)

    @classmethod
    def snapshot(cls):
        with cls._lock:
            now = time.time()
            for ip in list(cls._bans):
                if cls._bans[ip]["until"] <= now:
                    del cls._bans[ip]
            return [
                {
                    "ip": ip,
                    "reason": b["reason"],
                    "banned_at": time.strftime("%H:%M:%S",
                                               time.localtime(b["banned_at"])),
                    "expires_in": int(round(b["until"] - now)),
                    "hits": b["hits"],
                }
                for ip, b in cls._bans.items()
            ]

    # ---- internal -----------------------------------------------------------
    @classmethod
    def _ban_locked(cls, ip, reason):
        now = time.time()
        existing = cls._bans.get(ip)
        hits = (existing["hits"] + 1) if existing else 1
        cls._bans[ip] = {
            "until": now + cls.BAN_DURATION,
            "reason": reason,
            "banned_at": now,
            "hits": hits,
        }
        # reset flood counters for this IP
        cls._incoming.pop(ip, None)
        cls._conns.pop(ip, None)


###############################################################################
#  Asynchronous reverse-DNS resolver (single worker thread + cache)
###############################################################################
class DNSResolver:
    _lock = threading.Lock()
    _cache = {}          # ip -> hostname (str, "" if not resolvable)
    _pending = set()
    _queue = queue.Queue()
    _worker_started = False

    @classmethod
    def _ensure_worker(cls):
        # double-checked locking; cheap
        if cls._worker_started:
            return
        with cls._lock:
            if cls._worker_started:
                return
            cls._worker_started = True
            t = threading.Thread(target=cls._worker_loop,
                                 name="dns-resolver", daemon=True)
            t.start()

    @classmethod
    def _worker_loop(cls):
        while True:
            ip = cls._queue.get()
            if ip is None:
                return
            try:
                hostname = socket.gethostbyaddr(ip)[0]
            except Exception:
                hostname = ""
            with cls._lock:
                cls._cache[ip] = hostname
                cls._pending.discard(ip)

    @classmethod
    def get(cls, ip):
        """
            Returns cached hostname or "" — never blocks. On a cache miss
            the lookup is queued to the background worker.
        """
        cls._ensure_worker()
        with cls._lock:
            if ip in cls._cache:
                return cls._cache[ip]
            if ip in cls._pending:
                return ""
            cls._pending.add(ip)
        cls._queue.put(ip)
        return ""


###############################################################################
###############################################################################
class TelnetRequestHandler(socketserver.StreamRequestHandler):
    """
        Request handler used for multi threaded TCP server
        @see: socketserver.StreamRequestHandler

        Simultaneous connections are limited to MAX_CLIENTS via a semaphore.
        Extra clients get a short notice and are disconnected.
        Banned IPs are dropped silently before any slot is reserved.

        A per-client watcher thread reads whatever the client sends. It serves
        two purposes:
          * detects suspicious input / floods and triggers a ban,
          * detects client disconnects (EOF / socket error) and stops the
            player immediately, so the stats reflect reality right away.
        The watcher is started even when --no-ban is given, otherwise a
        graceful disconnect would not be noticed until the movie ends.
    """

    filename = None  # filename is set once, so it's immutable and safe for multi threading
    _client_semaphore = threading.Semaphore(MAX_CLIENTS)
    enable_ban = True

    def handle(self):
        ip, port = self.client_address[0], self.client_address[1]

        # 1) already banned? drop silently
        if TelnetRequestHandler.enable_ban and BanRegistry.get_ban(ip):
            return

        # 2) try to reserve a slot
        if not TelnetRequestHandler._client_semaphore.acquire(blocking=False):
            try:
                self.wfile.write(b"Server is full (max %d clients). Try again later.\r\n"
                                  % MAX_CLIENTS)
                self.wfile.flush()
            except Exception:
                pass
            return

        # 3) connection-flood check
        if TelnetRequestHandler.enable_ban:
            banned, reason = BanRegistry.record_connection(ip)
            if banned:
                sys.stderr.write("[ban] %s -> %s\n" % (ip, reason))
                TelnetRequestHandler._client_semaphore.release()
                return

        stats = StatsRegistry.register(ip, port)
        stop_event = threading.Event()
        self._stop_event = stop_event   # used by onNextFrame and _terminate

        # 4) watcher — always on, it also detects graceful disconnects
        watcher = threading.Thread(
            target=self._watch_input,
            args=(ip, stats, stop_event),
            name="input-watch-%s" % ip,
            daemon=True,
        )
        watcher.start()

        try:
            movie = Movie()
            movie.loadMovie(TelnetRequestHandler.filename)
            player = VT100Player(movie, stop_event=stop_event)
            player.onNextFrame = lambda buf, s=stats: self.onNextFrame(buf, s)
            player.play()
        finally:
            # stop player + unblock the watcher if it's still blocked in read()
            stop_event.set()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            StatsRegistry.unregister(stats)
            TelnetRequestHandler._client_semaphore.release()

    # ------------------------------------------------------------------ io
    def _terminate(self):
        """
            Ask the player to stop and unblock any pending socket I/O.
            Safe to call from any thread, idempotent.
        """
        try:
            self._stop_event.set()
        except Exception:
            pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

    def onNextFrame(self, screenBuffer, stats):
        """
            Gets the current screen buffer and writes it to the socket.
            Also accounts the transmitted bytes for the stats dashboard.
            On any I/O error the connection is considered dead and the
            player is stopped right away.
        """
        try:
            data = screenBuffer.read().encode("iso-8859-15")
            self.wfile.write(data)
            self.wfile.flush()
            StatsRegistry.add_bytes(stats, len(data))
        except Exception:
            # broken pipe / reset — do not keep pretending the client is here
            self._terminate()

    def _watch_input(self, ip, stats, stop_event):
        """
            Reads whatever the client sends. A healthy viewer sends nothing;
            a brute-forcer or a scanner will eventually emit payload.
            Regardless of the exit reason (EOF, ban, socket error), the
            player is stopped so the client disappears from "online" stats.
        """
        try:
            while not stop_event.is_set():
                try:
                    chunk = self.rfile.read1(256)
                except Exception:
                    break
                if not chunk:
                    # EOF: client closed the connection cleanly
                    break
                StatsRegistry.add_input(stats, len(chunk))
                if not TelnetRequestHandler.enable_ban:
                    continue
                banned, reason = BanRegistry.record_incoming(ip, chunk)
                if banned:
                    sys.stderr.write("[ban] %s -> %s\n" % (ip, reason))
                    break
        except Exception:
            pass
        finally:
            # in *every* exit path tell the player to stop and drop the socket
            stop_event.set()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

###############################################################################
class VT100Player:
    """
        Player class plays a movie. Offers higher methods for play, stop,
        fast forward and rewind on the movie. It also stores the current
        position.
        It exposes the all frame numbers in real values. Therefore not encoded.
    """
    __TIMEBAR = " <" + "".ljust(MAXDIM[0] - 4) + ">"

    def __init__(self, movie, stop_event=None):
        self.__movie = movie
        self.__movCursor = 0   # virtual cursor pointing to the current frame
        self.__maxFrames = 0
        self.stop_event = stop_event
        for f in self.__movie.getEncFrames():
            self.__maxFrames += f.displayTime

    def getDuration(self):
        """
            return the number of seconds this movie is playing
        """
        return self.__maxFrames // 15  # 15 frames per second

    def play(self):
        """
            plays the movie
        """
        for frame in self.__movie.getEncFrames():
            if self.stop_event is not None and self.stop_event.is_set():
                return
            self.__movCursor += frame.displayTime
            self.__onNextFrameInternal(frame, self.__movCursor)
            if not self._sleep_with_stop(frame.displayTime / 15.0):
                return

    def _sleep_with_stop(self, duration):
        """
            Sleeps, but wakes up early if stop_event gets set.
            Returns False if the player should stop.
        """
        if self.stop_event is None:
            time.sleep(duration)
            return True
        end = time.time() + duration
        while True:
            if self.stop_event.is_set():
                return False
            remaining = end - time.time()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

    def __onNextFrameInternal(self, frame, framePos):
        """
            internal event, happen when next frame should be drawn
        """
        screenbuf = StringIO()
        if framePos <= 1:
            screenbuf.write(VT100Codes.CLEARSCRN)

        # center vertical, with respect to the time bar
        y = (MAXDIM[1] - 1 - self.__movie.dimension[1]) // 2
        screenbuf.write(VT100Codes().JMPXY(1, y))

        for line in frame.data:
            screenbuf.write(line + "\r\n")

        self._updateTimeBar(screenbuf, framePos, self.__maxFrames)

        # now rewind the internal buffer and fire the public event
        screenbuf.seek(0)
        self.onNextFrame(screenbuf)

    def onNextFrame(self, screenBuffer):
        """
            Public event method, which can be used to get new Screens.

            @param screenBuffer: its a file like object containing the VT100 screen buffer
        """
        pass

    def _updateTimeBar(self, screenBuffer, intCurrentValue, intMaxSize=10):
        """
            Writes at the bottom of the screen a line like this
            <.......o.....................>
            Left and right are one blank spaces from the max screen dimensions
            It should visualize a timeline with 'o' is the current position.

            @param screenBuffer: file like object, where the data is written to
            @param intCurrentValue: current value
            @param intMaxSize: maximum value
        """
        screenBuffer.write(VT100Codes().JMPXY(1, MAXDIM[1]))
        screenBuffer.write(self.__TIMEBAR)

        denom = max(1, intMaxSize - 1)
        x = min(
            (intCurrentValue * (MAXDIM[0] - 4)) // denom,
            (MAXDIM[0] - 4 - 1)
        )
        screenBuffer.write(VT100Codes().JMPXY(x + 3, MAXDIM[1]))
        screenBuffer.write("o")


###############################################################################
#  Built-in HTML stats dashboard
###############################################################################
STATS_HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>ASCII Telnet Server — Monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0e1116; color: #e6edf3;
  }
  h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
  h2 { font-size: 15px; margin: 24px 0 8px; font-weight: 600; color: #c9d1d9; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .cards { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 8px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 16px; min-width: 150px;
  }
  .card .label { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  .card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
  .card.danger { border-color: #f85149; }
  .card.danger .value { color: #ff7b72; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #21262d; }
  th { color: #8b949e; font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  tr.active td { color: #e6edf3; }
  tr.idle td { color: #6e7681; }
  tr.banned td { color: #ffb3ad; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .dot.on { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  .dot.off { background: #484f58; }
  .dot.ban { background: #f85149; box-shadow: 0 0 6px #f85149; }
  .num { font-variant-numeric: tabular-nums; }
  .host { color: #8b949e; font-size: 12px; }
  button {
    background: #21262d; color: #e6edf3; border: 1px solid #30363d;
    border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px;
  }
  button:hover { background: #30363d; }
  button.danger { border-color: #f85149; color: #ff7b72; }
  button.danger:hover { background: #3d1418; }
  .status { color: #8b949e; font-size: 13px; }
  .empty { color: #8b949e; padding: 12px; }
  .reason { color: #ffb3ad; font-size: 12px; }
</style>
</head>
<body>
  <h1>ASCII Telnet Server — Monitor</h1>
  <div class="sub">Встроенная панель мониторинга. Данные обновляются раз в секунду. Хостнеймы подгружаются через reverse-DNS.</div>

  <div class="toolbar">
    <button id="toggle">&#9208; Пауза</button>
    <span class="status" id="status">обновление…</span>
  </div>

  <div class="cards">
    <div class="card"><div class="label">Uptime</div><div class="value num" id="uptime">–</div></div>
    <div class="card"><div class="label">Active / Max</div><div class="value num" id="active">–</div></div>
    <div class="card"><div class="label">Connections</div><div class="value num" id="totalconn">–</div></div>
    <div class="card"><div class="label">Bytes sent</div><div class="value num" id="totalbytes">–</div></div>
    <div class="card danger"><div class="label">Banned IPs</div><div class="value num" id="bancount">–</div></div>
  </div>

  <h2>Clients</h2>
  <table>
    <thead>
      <tr>
        <th>#</th><th>Status</th><th>IP / Hostname</th>
        <th>Connected at</th><th>Duration</th>
        <th>Frames</th><th>In</th><th>Out</th><th>Rate</th>
      </tr>
    </thead>
    <tbody id="rows">
      <tr><td class="empty" colspan="9">нет данных</td></tr>
    </tbody>
  </table>

  <h2>Banned IPs</h2>
  <table>
    <thead>
      <tr>
        <th>IP / Hostname</th><th>Reason</th><th>Banned at</th>
        <th>Expires in</th><th>Hits</th><th></th>
      </tr>
    </thead>
    <tbody id="banrows">
      <tr><td class="empty" colspan="6">забаненных нет</td></tr>
    </tbody>
  </table>

<script>
(function() {
  "use strict";
  const el = (id) => document.getElementById(id);
  let paused = false;

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }
  function fmtDuration(sec) {
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (x) => String(x).padStart(2, "0");
    return h ? (h + ":" + pad(m) + ":" + pad(s)) : (m + ":" + pad(s));
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function ipHost(ip, host) {
    const h = host ? ' <span class="host">/ ' + esc(host) + '</span>' : '';
    return esc(ip) + h;
  }

  async function refresh() {
    if (paused) return;
    try {
      const r = await fetch("/api/stats", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      render(d);
      el("status").textContent = "обновлено в " + d.server_time;
      el("status").style.color = "#8b949e";
    } catch (e) {
      el("status").textContent = "ошибка: " + e.message;
      el("status").style.color = "#f85149";
    }
  }

  function render(d) {
    el("uptime").textContent = fmtDuration(d.uptime);
    el("active").textContent = d.active + " / " + d.max_clients;
    el("totalconn").textContent = d.total_connections;
    el("totalbytes").textContent = fmtBytes(d.total_bytes);
    el("bancount").textContent = (d.banned ? d.banned.length : 0);

    // ---- clients ----
    const rows = el("rows");
    if (!d.clients.length) {
      rows.innerHTML = '<tr><td class="empty" colspan="9">нет данных</td></tr>';
    } else {
      const clients = d.clients.slice().sort((a, b) => {
        if (a.active !== b.active) return a.active ? -1 : 1;
        return b.id - a.id;
      });
      let html = "";
      for (const c of clients) {
        const rate = c.duration > 0 ? (c.bytes_sent / c.duration) : 0;
        html += '<tr class="' + (c.active ? "active" : "idle") + '">'
          + '<td class="num">' + c.id + '</td>'
          + '<td><span class="dot ' + (c.active ? "on" : "off") + '"></span>'
          + (c.active ? "online" : "closed") + '</td>'
          + '<td>' + ipHost(c.ip, c.hostname) + '</td>'
          + '<td class="num">' + esc(c.connected_at) + '</td>'
          + '<td class="num">' + fmtDuration(c.duration) + '</td>'
          + '<td class="num">' + c.frames_sent + '</td>'
          + '<td class="num">' + fmtBytes(c.input_bytes || 0) + '</td>'
          + '<td class="num">' + fmtBytes(c.bytes_sent) + '</td>'
          + '<td class="num">' + fmtBytes(rate) + '/s</td>'
          + '</tr>';
      }
      rows.innerHTML = html;
    }

    // ---- bans ----
    const brows = el("banrows");
    if (!d.banned || !d.banned.length) {
      brows.innerHTML = '<tr><td class="empty" colspan="6">забаненных нет</td></tr>';
    } else {
      let html = "";
      for (const b of d.banned) {
        html += '<tr class="banned">'
          + '<td>' + ipHost(b.ip, b.hostname) + '</td>'
          + '<td class="reason">' + esc(b.reason) + '</td>'
          + '<td class="num">' + esc(b.banned_at) + '</td>'
          + '<td class="num">' + fmtDuration(b.expires_in) + '</td>'
          + '<td class="num">' + b.hits + '</td>'
          + '<td><button class="danger" data-ip="' + esc(b.ip) + '">Unban</button></td>'
          + '</tr>';
      }
      brows.innerHTML = html;
      brows.querySelectorAll("button[data-ip]").forEach(btn => {
        btn.addEventListener("click", async () => {
          const ip = btn.getAttribute("data-ip");
          try {
            await fetch("/api/unban?ip=" + encodeURIComponent(ip),
                        { cache: "no-store" });
          } catch (e) {}
          refresh();
        });
      });
    }
  }

  el("toggle").addEventListener("click", () => {
    paused = !paused;
    el("toggle").innerHTML = paused ? "&#9654; Продолжить" : "&#9208; Пауза";
    if (!paused) refresh();
  });

  refresh();
  setInterval(refresh, 1000);
})();
</script>
</body>
</html>
"""


class StatsHTTPHandler(http.server.BaseHTTPRequestHandler):
    """
        Minimal HTTP handler:
            /              -> HTML dashboard
            /api/stats     -> JSON snapshot (clients + bans)
            /api/unban?ip= -> lift a ban, returns {"ok":true}
    """

    server_version = "AsciiTelnetStats/1.1"

    def log_message(self, fmt, *args):
        # keep the console clean, we already print our own messages
        pass

    def _send(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path, _, query = self.path.partition("?")

        if path in ("/", "/index.html", "/index.htm"):
            self._send(STATS_HTML_PAGE.encode("utf-8"),
                       "text/html; charset=utf-8")

        elif path == "/api/stats":
            snap = StatsRegistry.snapshot()
            snap["banned"] = BanRegistry.snapshot()
            # resolve hostnames outside of locks
            for c in snap["clients"]:
                c["hostname"] = DNSResolver.get(c["ip"])
            for b in snap["banned"]:
                b["hostname"] = DNSResolver.get(b["ip"])
            data = json.dumps(snap, ensure_ascii=False).encode("utf-8")
            self._send(data, "application/json; charset=utf-8")

        elif path == "/api/unban":
            params = urllib.parse.parse_qs(query)
            ip = (params.get("ip") or [""])[0].strip()
            if ip:
                BanRegistry.unban(ip)
            self._send(b'{"ok":true}', "application/json; charset=utf-8")

        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def runStatsServer(interface, port):
    """
        Starts the stats HTTP server in a daemon background thread.
        Returns the server object or None if the port could not be bound.
    """
    try:
        httpd = http.server.ThreadingHTTPServer(
            (interface, port), StatsHTTPHandler
        )
    except OSError as e:
        sys.stderr.write(
            "Warning: cannot bind stats HTTP server on %s:%d (%s)\n"
            % (interface, port, e)
        )
        return None

    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever,
                         name="stats-http", daemon=True)
    t.start()
    return httpd


###############################################################################
def runTcpServer(interface, port, filename):
    """
        @param interface: bind to this interface
        @param port: bind to this port
        @param filename: file name of the ASCII movie
    """
    TelnetRequestHandler.filename = filename

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True          # не держать процесс при выходе
        request_queue_size = 50        # очередь ядра больше лимита клиентов

    server = Server((interface, port), TelnetRequestHandler)
    try:
        server.serve_forever()
    except Exception:
        pass  # if some one cancels the player, we don't care


###############################################################################
def onNextFrameStdOut(screenBuffer):
    sys.stdout.write(screenBuffer.read())
    sys.stdout.flush()


def runStdOut(filename):
    """
        @param filename: file name of the ASCII movie
    """
    movie = Movie()
    movie.loadMovie(filename)
    player = VT100Player(movie)
    player.onNextFrame = onNextFrameStdOut
    try:
        player.play()
    except Exception:
        pass  # if some one cancels the player, we don't care


### MAIN ######################################################################
def main():
    usage = "%(prog)s [options]"
    parser = argparse.ArgumentParser(usage=usage)

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--standalone", dest="tcpserv", action="store_true",
        help="Run as stand alone multi threaded TCP server (default)"
    )
    group.add_argument(
        "--stdout", dest="tcpserv", action="store_false",
        help="Run with STDIN and STDOUT, for example in XINETD "
             "instead of stand alone TCP server. "
             "Use with python option '-u' for unbuffered "
             "STDIN STDOUT communication"
    )

    parser.add_argument(
        "-f", "--file", dest="filename", metavar="FILE",
        help="Text file containing the ASCII movie"
    )
    parser.add_argument(
        "-i", "--interface", dest="interface",
        help="Bind to this interface (default '0.0.0.0', all interfaces)",
        default="0.0.0.0"
    )
    parser.add_argument(
        "-p", "--port", dest="port", metavar="PORT",
        help="Bind to this port (default 23, Telnet)",
        default=23, type=int
    )

    # --- stats HTTP server options -------------------------------------------
    parser.add_argument(
        "--http-interface", dest="http_interface",
        help="Bind the stats HTTP server to this interface "
             "(default '0.0.0.0')",
        default="0.0.0.0"
    )
    parser.add_argument(
        "--http-port", dest="http_port", metavar="PORT", type=int,
        help="Bind the stats HTTP server to this port (default 8080)",
        default=8080
    )
    parser.add_argument(
        "--no-http", dest="no_http", action="store_true",
        help="Disable the built-in HTML stats dashboard"
    )

    # --- ban options ---------------------------------------------------------
    parser.add_argument(
        "--no-ban", dest="no_ban", action="store_true",
        help="Disable the built-in heuristic IP auto-ban "
             "(incoming payload / connection flood)"
    )
    parser.add_argument(
        "--ban-duration", dest="ban_duration", metavar="SEC", type=int,
        default=int(BanRegistry.BAN_DURATION),
        help="How long (seconds) an offending IP stays banned "
             "(default %d)" % int(BanRegistry.BAN_DURATION)
    )

    vgroup = parser.add_mutually_exclusive_group()
    vgroup.add_argument(
        "-v", "--verbose", action="store_true", dest="verbose",
        help="Verbose (default for TCP server)"
    )
    vgroup.add_argument(
        "-q", "--quiet", action="store_false", dest="verbose",
        help="Quiet! (default for STDIN STDOUT server)"
    )

    parser.set_defaults(
        interface="0.0.0.0",
        port=23,
        tcpserv=True,
        verbose=True,
        http_interface="0.0.0.0",
        http_port=8080,
        no_http=False,
        no_ban=False,
        ban_duration=int(BanRegistry.BAN_DURATION),
    )

    options = parser.parse_args()

    if not (options.filename and os.path.exists(options.filename)):
        parser.exit(1, "Error, file not found! See --help for details.\n")

    # configure ban subsystem
    BanRegistry.BAN_DURATION = float(options.ban_duration)
    TelnetRequestHandler.enable_ban = not options.no_ban

    if options.tcpserv:
        if options.verbose:
            print("Running TCP server on %s:%d" % (options.interface, options.port))
            print("Playing movie " + options.filename)
            if options.no_ban:
                print("Auto-ban: disabled")
            else:
                print("Auto-ban: on (duration %ds)" % int(BanRegistry.BAN_DURATION))

        if not options.no_http:
            httpd = runStatsServer(options.http_interface, options.http_port)
            if httpd is not None and options.verbose:
                print("Stats dashboard on http://%s:%d/"
                      % (options.http_interface, options.http_port))

        runTcpServer(options.interface, options.port, options.filename)
    else:
        runStdOut(options.filename)

    sys.exit(0)


if __name__ == "__main__":
    main()
