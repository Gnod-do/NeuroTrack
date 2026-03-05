# -*- coding: utf-8 -*-
"""
Мост NeuroTrack ↔ Trackduino
Ключевые требования, реализованные в этой версии:
- кроссплатформенный вывод портов (без обязательного winreg);
- чтение NeuroTrack (TGAM EEG 2.9 / ThinkGear) + PoorSignal (0x02);
- статусы: Подключение..., Подключено, Плохой контакт, Нейротрек снят или плохой контакт, Нет сигнала, Отключено;
- моргание приводится к диапазону 0–100;
- обмен с Trackduino строго по протоколу TrackduinoRemote:
  Trackduino -> ПК: <{"Nreq":X}>
  ПК -> Trackduino: <{"n":{"a":A,"m":M,"b":B}}>
  При потере сигнала/контакта: A=M=B=0;
- обновление списка портов не чаще 1 раза в 10 секунд и без сброса выбранных портов;
- уведомления (небольшие окна) на 20 секунд при отключении питания/потере порта/потере контакта.
"""

import os
import sys
import time
import json
import threading
import platform
import socket
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urllib_request
from typing import List, Tuple, Optional, Dict
import queue

import tkinter as tk
from tkinter import ttk, messagebox
import serial
import serial.tools.list_ports
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

# --- winreg НЕ обязателен. Используем только если доступен (Windows). ---
try:
    import winreg  # type: ignore
except Exception:
    winreg = None  # noqa


# ==============================
# --- ThinkGear (TGAM) parser ---
# ==============================
class ThinkGearParser:
    """
    Достаёт payload из потока байтов ThinkGear (AA AA LEN PAYLOAD CHK).
    Проверка checksum выполняется (как в протоколе ThinkGear).
    """

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes):
        self.buffer.extend(data)

    @staticmethod
    def _valid_checksum(payload: bytes, checksum: int) -> bool:
        return ((~(sum(payload) & 0xFF)) & 0xFF) == (checksum & 0xFF)

    def get_packets(self) -> List[bytes]:
        packets: List[bytes] = []

        while len(self.buffer) >= 4:
            # AA AA
            if not (self.buffer[0] == 0xAA and self.buffer[1] == 0xAA):
                self.buffer.pop(0)
                continue

            if len(self.buffer) < 3:
                break

            length = self.buffer[2]
            if length > 169:  # ThinkGear payload max обычно 169
                # Сдвигаемся, если мусор
                self.buffer.pop(0)
                continue

            need = 3 + length + 1
            if len(self.buffer) < need:
                break

            payload = bytes(self.buffer[3:3 + length])
            checksum = int(self.buffer[3 + length])

            # сдвигаем буфер (AA AA LEN + PAYLOAD + CHK)
            del self.buffer[:need]

            if not self._valid_checksum(payload, checksum):
                # пропускаем битые пакеты
                continue

            packets.append(payload)

        return packets


# ==============================
# --- COM ports helpers ---
# ==============================
def list_com_ports() -> List[Tuple[str, str]]:
    """
    Кроссплатформенно выводит доступные порты.
    На Windows дополнительно пытается улучшить имена BT-устройств через реестр (если winreg доступен).
    На Linux просто использует pyserial list_ports (как есть).
    """
    ports = list(serial.tools.list_ports.comports())

    # На Linux/Unix pyserial обычно уже даёт нормальные description.
    # На Windows можно попробовать подтянуть FriendlyName для BT.
    bt_name_by_tail: Dict[str, str] = {}

    if winreg is not None:
        try:
            R_ACCESS = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            REG_PATH = r"SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Devices"

            def _decode_name(raw) -> str:
                try:
                    if isinstance(raw, (bytes, bytearray)):
                        # часто имя в UTF-16LE или ascii-похоже
                        try:
                            return raw.decode("utf-8", errors="ignore").strip("\x00")
                        except Exception:
                            return raw.decode("ascii", errors="ignore").strip("\x00")
                    return str(raw)
                except Exception:
                    return str(raw)

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_PATH, 0, R_ACCESS) as main:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(main, i)
                        i += 1
                        with winreg.OpenKey(main, sub, 0, R_ACCESS) as dev:
                            for key in ("Name", "FriendlyName", "LEName"):
                                try:
                                    val, _ = winreg.QueryValueEx(dev, key)
                                    name = _decode_name(val)
                                    if name:
                                        bt_name_by_tail[sub[-12:].upper()] = name
                                        break
                                except FileNotFoundError:
                                    continue
                    except OSError:
                        break
        except Exception:
            bt_name_by_tail = {}

    out: List[Tuple[str, str]] = []
    for p in ports:
        dev = p.device
        name = p.description or dev

        hwid = (p.hwid or "").upper()
        if bt_name_by_tail:
            for tail, fname in bt_name_by_tail.items():
                if tail in hwid:
                    name = fname
                    break

        # фильтр "стандартных" / пустых
        if "СТАНДАРТНЫЙ" in (name or "").upper():
            continue

        out.append((dev, name))

    # Стабильная сортировка по имени устройства нужна для Linux-систем,
    # где последовательность list_ports может «плавать» между вызовами.
    out.sort(key=lambda item: item[0])

    return out


def serial_open_kwargs(timeout: float = 0.1) -> Dict[str, object]:
    """
    Возвращает аргументы для serial.Serial без платформенных regressions.
    - Windows: используем только timeout (поведение как раньше).
    - Linux/*nix: отключаем эксклюзивный lock порта (exclusive=False),
      чтобы избежать ошибок открытия на некоторых дистрибутивах.
    """
    kwargs: Dict[str, object] = {"timeout": timeout}
    if platform.system().lower() != "windows":
        kwargs["exclusive"] = False
    return kwargs


# ==============================
# --- UI widgets (Tkinter) ---
# ==============================
class Toast(tk.Toplevel):
    """
    Небольшое уведомление, показывается на 20 секунд и исчезает.
    """

    def __init__(self, parent, title: str, text: str, timeout_ms: int = 20000):
        super().__init__(parent)
        self.title(title)
        self.attributes("-topmost", True)
        self.resizable(False, False)

        # Центрируем относительно родителя
        if parent:
            x = parent.winfo_x() + parent.winfo_width() // 2 - 210
            y = parent.winfo_y() + parent.winfo_height() // 2 - 60
            self.geometry(f"420x120+{x}+{y}")

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        label = ttk.Label(frame, text=text, wraplength=380, justify=tk.LEFT)
        label.pack(fill=tk.BOTH, expand=True)

        self.after(timeout_ms, self.destroy)


class VerticalBar(ttk.Frame):
    """
    Вертикальный прогресс-бар, который заполняется СНИЗУ ВВЕРХ.
    """

    def __init__(self, parent, title: str, color: str = "#00ff99"):
        super().__init__(parent)

        self.title = title
        self.color = color
        self.value = 0

        # Заголовок
        self.title_label = ttk.Label(self, text=title, anchor=tk.CENTER)
        self.title_label.pack(side=tk.TOP, fill=tk.X)

        # Контейнер для прогресс-бара
        self.canvas_frame = ttk.Frame(self, width=60, height=200)
        self.canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=5)
        self.canvas_frame.pack_propagate(False)

        # Canvas для рисования вертикального прогресс-бара
        self.canvas = tk.Canvas(self.canvas_frame, bg="#18191e", highlightthickness=1,
                                highlightbackground="#30303a")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # Значение
        self.value_label = ttk.Label(self, text="0%", anchor=tk.CENTER)
        self.value_label.pack(side=tk.BOTTOM, fill=tk.X)

        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event=None):
        self.update_bar(self.value)

    def set_value(self, value: int):
        self.value = max(0, min(100, value))
        self.update_bar(self.value)

    def update_bar(self, value: int):
        self.value_label.config(text=f"{value}%")

        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()

        if width > 1 and height > 1:
            self.canvas.delete("bar")

            # Рисуем фон
            self.canvas.create_rectangle(0, 0, width, height, fill="#18191e", outline="")

            # Рисуем заполнение снизу вверх
            bar_height = int(height * value / 100)
            y1 = height - bar_height
            self.canvas.create_rectangle(0, y1, width, height,
                                         fill=self.color, outline="", tags="bar")


# ==============================
# --- Поток чтения NeuroTrack ---
# ==============================
class NeuroReader(threading.Thread):
    """
    Читает ThinkGear пакеты и выдаёт:
    attention (0–100), meditation (0–100), blink (0–100), poor_signal (0–200)
    """

    def __init__(self, port: str, callback_queue):
        super().__init__(daemon=True)
        self.port = port
        self.callback_queue = callback_queue
        self._run = True
        self._has_first_packet = False
        self._last_data_ts = time.time()

        self._last_status = ""

        # Отладка blink-кода (0x16): включается через переменную среды NT_DEBUG_BLINK=1
        self._debug_blink = os.getenv("NT_DEBUG_BLINK", "0").strip().lower() in {"1", "true", "yes", "on"}
        self._last_blink_debug_ts = 0.0

        # Fallback-detect моргания по RAW (0x80), если чип редко/нестабильно шлёт 0x16.
        self._raw_blink_threshold = int(os.getenv("NT_BLINK_RAW_THRESHOLD", "1000"))
        self._raw_blink_debounce = 0.3
        self._last_blink_ts = 0.0

    def stop(self):
        self._run = False

    def _emit_status(self, s: str):
        if s != self._last_status:
            self._last_status = s
            self.callback_queue.put(("neuro_status", s))

    @staticmethod
    def _blink_to_0_100(v: int) -> int:
        # TGAM blink обычно 0..255, приводим к 0..100
        if v <= 0:
            return 0
        if v >= 255:
            return 100
        return int(round(v * 100.0 / 255.0))

    def _debug_log_blink(self, payload: bytes, had_blink_code: bool, blink_raw: Optional[int]):
        if not self._debug_blink:
            return

        now = time.time()
        if had_blink_code:
            self._last_blink_debug_ts = now
            print(f"[BLINK-DEBUG] code=0x16 raw={blink_raw} mapped={self._blink_to_0_100(int(blink_raw or 0))}")
            return

        # Чтобы не спамить: не чаще раза в 2 секунды пишем отсутствие 0x16.
        if now - self._last_blink_debug_ts >= 2.0:
            self._last_blink_debug_ts = now
            hex_payload = payload.hex(" ")
            print(f"[BLINK-DEBUG] no 0x16 in payload (len={len(payload)}): {hex_payload}")

    @staticmethod
    def _raw_to_blink_0_100(raw_value: int) -> int:
        # Мягкое отображение амплитуды RAW -> 0..100
        return int(max(0, min(100, abs(raw_value) / 20.0)))

    def _detect_blink_from_raw(self, raw_value: int) -> Optional[int]:
        now = time.time()
        if abs(raw_value) < self._raw_blink_threshold:
            return None
        if now - self._last_blink_ts < self._raw_blink_debounce:
            return None
        self._last_blink_ts = now
        return self._raw_to_blink_0_100(raw_value)

    def run(self):
        try:
            self._emit_status("Подключение...")
            ser = None
            for attempt in range(3):
                try:
                    ser = serial.Serial(self.port, 57600, **serial_open_kwargs(timeout=0.1))
                    break
                except Exception:
                    time.sleep(0.7)

            if ser is None:
                self._emit_status("Отключено")
                return
        except Exception:
            self._emit_status("Отключено")
            return

        parser = ThinkGearParser()

        attention = 0
        meditation = 0
        blink_0_100 = 0
        poor = 200  # по умолчанию "плохой"

        while self._run:
            try:
                data = ser.read(64)
                if data:
                    parser.feed(data)
                    packets = parser.get_packets()

                    for payload in packets:
                        i = 0
                        had_blink_code = False
                        blink_raw: Optional[int] = None
                        while i < len(payload):
                            code = payload[i]
                            i += 1

                            # extended code 0x55: пропускаем и читаем следующий код
                            if code == 0x55:
                                continue

                            # PoorSignal (0x02)
                            if code == 0x02 and i < len(payload):
                                poor = payload[i]
                                i += 1
                                continue

                            # Attention (0x04)
                            if code == 0x04 and i < len(payload):
                                attention = payload[i]
                                i += 1
                                continue

                            # Meditation (0x05)
                            if code == 0x05 and i < len(payload):
                                meditation = payload[i]
                                i += 1
                                continue

                            # Blink strength (0x16)
                            if code == 0x16 and i < len(payload):
                                blink_raw = int(payload[i])
                                had_blink_code = True
                                blink_0_100 = self._blink_to_0_100(blink_raw)
                                i += 1
                                continue

                            # RAW EEG value (0x80): [len=2][hi][lo], signed
                            if code == 0x80 and i < len(payload):
                                ln = payload[i]
                                i += 1
                                if i + ln <= len(payload):
                                    raw_bytes = payload[i:i + ln]
                                    i += ln
                                    if ln > 0:
                                        raw = int.from_bytes(raw_bytes, byteorder="big", signed=True)
                                        raw_blink = self._detect_blink_from_raw(raw)
                                        if raw_blink is not None:
                                            blink_0_100 = raw_blink
                                else:
                                    i = len(payload)
                                continue

                            # multi-byte values: code >= 0x80, next is length
                            if code >= 0x80 and i < len(payload):
                                ln = payload[i]
                                i += 1 + ln
                                continue

                            # single-byte unknown
                            # nothing else to do

                        self._debug_log_blink(payload, had_blink_code, blink_raw)

                        self._last_data_ts = time.time()
                        if not self._has_first_packet:
                            self._has_first_packet = True

                        # статус по poor
                        if poor <= 5:
                            status = "Подключено"
                        elif poor <= 50:
                            status = "Нормальный контакт"
                        elif poor <= 150:
                            status = "Плохой контакт"
                        else:
                            status = "Нейротрек снят"

                        self._emit_status(status)
                        self.callback_queue.put(("neuro_sample", attention, meditation, blink_0_100, poor))

                else:
                    elapsed = time.time() - self._last_data_ts
                    if not self._has_first_packet:
                        self._emit_status("Подключение...")
                    elif elapsed > 10:
                        self._emit_status("Нет сигнала")
                    elif elapsed > 5:
                        # если раньше poor==0, но теперь тишина – считаем плохим контактом
                        self._emit_status("Плохой контакт")

            except Exception:
                self._emit_status("Отключено")
                break

        try:
            ser.close()
        except Exception:
            pass

        self._emit_status("Отключено")


# ==============================
# --- Поток Trackduino (Nreq) ---
# ==============================
class TrackBridge(threading.Thread):
    """
    Открывает порт Trackduino и отвечает на запросы Nreq:
    Trackduino -> ПК: <{"Nreq":X}>
    ПК -> Trackduino: <{"n":{"a":A,"m":M,"b":B}}>
    """

    def __init__(self, port: str, callback_queue, baud: int = 115200):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.callback_queue = callback_queue
        self._run = True
        self._lock = threading.Lock()

        self._a = 0
        self._m = 0
        self._b = 0
        self._neuro_ok = False  # poor == 0 и есть свежие данные

        self._last_req_ts = 0.0

        self._buf = bytearray()
        self._last_data_ts = time.time()
        self._timeout_sec = 5.0  # сколько секунд ждём данные
        self._last_status = ""

        self._got_first_request = False

    def stop(self):
        self._run = False

    def _emit_status(self, s: str):
        if s != self._last_status:
            self._last_status = s
            self.callback_queue.put(("track_status", s))

    def set_neuro_values(self, a: int, m: int, b: int, neuro_ok: bool):
        with self._lock:
            self._a, self._m, self._b = a, m, b
            self._neuro_ok = neuro_ok

    def _build_reply(self) -> bytes:
        with self._lock:
            if self._neuro_ok:
                a, m, b = int(self._a), int(self._m), int(self._b)
            else:
                a = m = b = 0
        msg = json.dumps({"n": {"a": a, "m": m, "b": b}}, separators=(",", ":"))
        return f"<{msg}>".encode("utf-8")

    def _feed_and_extract_frames(self, data: bytes) -> List[bytes]:
        frames: List[bytes] = []
        self._buf.extend(data)

        while True:
            try:
                start = self._buf.index(ord("<"))
            except ValueError:
                # нет начала
                self._buf.clear()
                break

            if start > 0:
                del self._buf[:start]

            try:
                end = self._buf.index(ord(">"), 1)
            except ValueError:
                # ждём дальше
                break

            frame = bytes(self._buf[1:end])  # без < >
            del self._buf[:end + 1]
            if frame:
                frames.append(frame)

        return frames

    def run(self):
        try:
            ser = serial.Serial(self.port, self.baud, **serial_open_kwargs(timeout=0.1))
            self.callback_queue.put(("track_opened", True, "Trackduino: подключено"))
            self._emit_status("Подключено")
        except Exception as e:
            self.callback_queue.put(("track_opened", False, f"Trackduino: ошибка открытия порта ({e})"))
            self._emit_status("Отключено")
            return

        while self._run:
            try:
                data = ser.read(128)

                if data:
                    for frame in self._feed_and_extract_frames(data):
                        try:
                            obj = json.loads(frame.decode("utf-8", errors="ignore"))
                        except Exception:
                            continue

                        if isinstance(obj, dict) and "Nreq" in obj:
                            ser.write(self._build_reply())
                            ser.flush()

            except Exception:
                self.callback_queue.put(("track_opened", False, "Trackduino: отключено (потеря порта)"))
                self._emit_status("Отключено")
                break

        try:
            ser.close()
        except Exception:
            pass

        self._emit_status("Отключено")


# ==============================
# --- Localhost data server ---
# ==============================
class LocalhostDataServer:
    """
    HTTP-сервер для выдачи текущих данных NeuroTrack на localhost.
    GET  /stream   -> веб-страница с live-обновлением данных
    GET  /stream/events -> та же веб-страница live-view
    GET  /stream/data -> непрерывный SSE-поток JSON
    POST /shutdown -> остановка сервера
    """

    def __init__(self, data_provider, host: str = "127.0.0.1", port: int = 8765, stream_interval: float = 0.2):
        self.host = host
        self.port = int(port)
        self.stream_interval = max(0.05, float(stream_interval))
        self._data_provider = data_provider
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _is_port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            try:
                sock.connect((self.host, self.port))
                return True
            except (OSError, ValueError):
                return False

    def _stop_existing_server(self):
        # Пытаемся корректно остановить прежний экземпляр на том же порту.
        try:
            req = urllib_request.Request(
                f"http://{self.host}:{self.port}/shutdown",
                data=b"",
                method="POST",
            )
            urllib_request.urlopen(req, timeout=2).read()
        except Exception:
            return

        for _ in range(10):
            if not self._is_port_in_use():
                break
            time.sleep(0.2)

    def start(self):
        if self.is_running():
            return

        if self._is_port_in_use():
            self._stop_existing_server()

        owner = self

        class ResultsHandler(BaseHTTPRequestHandler):
            def _send_json(self, payload: Dict[str, object], status: int = 200):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _send_html(self, html: str, status: int = 200):
                raw = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _stream_sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                while owner.is_running():
                    payload = owner._data_provider()
                    event = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                    try:
                        self.wfile.write(event)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    time.sleep(owner.stream_interval)

            def _stream_page_html(self) -> str:
                return """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NeuroTrack • Live Dashboard</title>
<style>
:root{--bg:#06070b;--panel:#0f1320cc;--line:#2a3652;--fg:#e8eeff;--muted:#98a7cc;--ok:#38d39f;--warn:#ff8a7a;--att1:#34d399;--att2:#10b981;--med1:#60a5fa;--med2:#3b82f6;--bl1:#ff9d5c;--bl2:#ef4444}
*{box-sizing:border-box}
body{margin:0;color:var(--fg);font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:
radial-gradient(900px 500px at 10% -20%, #1e2f5f 0%, transparent 60%),
radial-gradient(1000px 600px at 110% 10%, #4a2c47 0%, transparent 60%),
linear-gradient(170deg,#05070d,#0a1021 45%,#090d17)}
.wrap{max-width:1150px;margin:20px auto;padding:0 16px}
.top{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.title{font-weight:900;font-size:31px;letter-spacing:.02em}
.subtitle{font-size:13px;color:var(--muted)}
.badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{padding:7px 11px;border-radius:999px;border:1px solid var(--line);background:#0e1628;font-size:12px;color:#bcd0ff}
.badge.ok{background:#0f2a22;border-color:#2d7e67;color:#8ff0ce}
.badge.err{background:#311922;border-color:#7f4052;color:#ffc9d4}
.grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px;backdrop-filter: blur(6px);box-shadow:0 12px 40px rgba(0,0,0,.35)}
.metric{grid-column:span 4}
@media (max-width:920px){.metric{grid-column:span 12}}
.name{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin-bottom:8px}
.value{font-size:44px;font-weight:900;line-height:1;margin-bottom:8px}
.track{height:10px;background:#1d2942;border-radius:999px;overflow:hidden}
.fill{height:100%;width:0;border-radius:999px;transition:width .12s linear}
.att{background:linear-gradient(90deg,var(--att1),var(--att2))}
.med{background:linear-gradient(90deg,var(--med1),var(--med2))}
.bli{background:linear-gradient(90deg,var(--bl1),var(--bl2))}
.side{grid-column:span 12}
.meta{display:flex;justify-content:space-between;gap:14px;align-items:center;color:var(--muted);font-size:13px;margin-bottom:10px;flex-wrap:wrap}
pre{margin:0;background:#0a1224;border:1px solid #213459;border-radius:12px;padding:12px;max-height:280px;overflow:auto;color:#d6e6ff}
.kv{display:flex;gap:10px;align-items:center}
.dot{width:10px;height:10px;border-radius:50%;background:#667aab}
.dot.ok{background:var(--ok)}
.dot.err{background:var(--warn)}
.hint{font-size:12px;color:var(--muted);margin-top:8px}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <div class="title">NeuroTrack • Live Dashboard</div>
      <div class="subtitle">Поток: <code>/stream/data</code> • Формат: <code>{"n":{"a":A,"m":M,"b":B}}</code></div>
    </div>
    <div class="badges">
      <div id="conn" class="badge">connecting…</div>
      <div id="blinkTx" class="badge">blink: waiting…</div>
    </div>
  </div>

  <div class="grid">
    <section class="card metric">
      <div class="name">Attention (a)</div>
      <div id="aVal" class="value">0%</div>
      <div class="track"><div id="aBar" class="fill att"></div></div>
    </section>

    <section class="card metric">
      <div class="name">Meditation (m)</div>
      <div id="mVal" class="value">0%</div>
      <div class="track"><div id="mBar" class="fill med"></div></div>
    </section>

    <section class="card metric">
      <div class="name">Blink (b)</div>
      <div id="bVal" class="value">0%</div>
      <div class="track"><div id="bBar" class="fill bli"></div></div>
    </section>

    <section class="card side">
      <div class="meta">
        <div class="kv"><span id="pktDot" class="dot"></span><span id="pktInfo">Нет входящих событий</span></div>
        <div>Последнее обновление: <b id="updated">—</b></div>
      </div>
      <pre id="raw">Ожидание данных…</pre>
      <div class="hint">Если поле <code>n.b</code> отсутствует, индикатор blink станет красным.</div>
    </section>
  </div>
</div>

<script>
const clamp=v=>Math.max(0,Math.min(100,Number(v)||0));
const raw=document.getElementById('raw');
const conn=document.getElementById('conn');
const blinkTx=document.getElementById('blinkTx');
const updated=document.getElementById('updated');
const pktDot=document.getElementById('pktDot');
const pktInfo=document.getElementById('pktInfo');
const aVal=document.getElementById('aVal');
const mVal=document.getElementById('mVal');
const bVal=document.getElementById('bVal');
const aBar=document.getElementById('aBar');
const mBar=document.getElementById('mBar');
const bBar=document.getElementById('bBar');

function setBadge(el,text,kind){el.textContent=text;el.classList.remove('ok','err');if(kind)el.classList.add(kind)}

function paint(obj){
  const n=(obj&&obj.n)?obj.n:{};
  const hasBlink=Object.prototype.hasOwnProperty.call(n,'b');
  const a=clamp(n.a), m=clamp(n.m), b=clamp(n.b);

  aVal.textContent=`${a}%`; mVal.textContent=`${m}%`; bVal.textContent=`${b}%`;
  aBar.style.width=`${a}%`; mBar.style.width=`${m}%`; bBar.style.width=`${b}%`;

  raw.textContent=JSON.stringify(obj,null,2);
  updated.textContent=new Date().toLocaleTimeString();
  pktDot.classList.remove('err'); pktDot.classList.add('ok');
  pktInfo.textContent='Пакет получен';

  if(hasBlink){
    setBadge(blinkTx,`blink: OK (${b}%)`,'ok');
  }else{
    setBadge(blinkTx,'blink: missing field b','err');
    pktDot.classList.remove('ok'); pktDot.classList.add('err');
    pktInfo.textContent='Внимание: в пакете нет поля n.b';
  }
}

const es=new EventSource('/stream/data');
es.onopen=()=>setBadge(conn,'live','ok');
es.onmessage=(ev)=>{try{paint(JSON.parse(ev.data));}catch(_){raw.textContent=ev.data;}};
es.onerror=()=>setBadge(conn,'reconnecting…','err');
</script>
</body>
</html>"""

            def do_GET(self):
                if self.path in ("/stream", "/stream/events"):
                    self._send_html(self._stream_page_html())
                elif self.path == "/stream/data":
                    self._stream_sse()
                else:
                    self._send_json({"error": "not found"}, status=404)

            def do_POST(self):
                if self.path == "/shutdown":
                    self._send_json({"status": "shutting down"})
                    threading.Thread(target=owner.stop, daemon=True).start()
                else:
                    self._send_json({"error": "not found"}, status=404)

            def log_message(self, format, *args):
                # подавляем стандартный вывод BaseHTTPRequestHandler
                return

        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), ResultsHandler)
        except Exception as e:
            print(f"Не удалось запустить localhost-сервер {self.host}:{self.port}: {e}")
            self._httpd = None
            self._thread = None
            return

        def run_server():
            assert self._httpd is not None
            try:
                self._httpd.serve_forever(poll_interval=0.2)
            finally:
                try:
                    self._httpd.server_close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=run_server, daemon=True)
        self._thread.start()
        print(f"Localhost-сервер запущен: http://{self.host}:{self.port}/stream, /stream/events и /stream/data")

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._httpd = None


# ==============================
# --- UI (Tkinter Main Window) ---
# ==============================
def nice_state_color(s: str) -> str:
    return {
        "Нормальный контакт": "#66cc00",
        "Normal contact": "#66cc00",
        "Нейротрек снят": "#ff3333",
        "NeuroTrack removed": "#ff3333",
        "Отключено": "#ff3333",
        "Disconnected": "#ff3333",
        "Нет сигнала": "#ff3333",
        "No signal": "#ff3333",
        "Подключение...": "#ffcc00",
        "Connecting...": "#ffcc00",
        "Плохой контакт": "#ffcc00",
        "Poor contact": "#ffcc00",
        "Подключено": "#00cc66",
        "Connected": "#00cc66",
    }.get(s, "#ff3333")


class BridgeUI:
    def __init__(self, root):
        self.root = root
        sys_locale = os.environ.get('LANG', 'ru_RU').lower()
        self.current_lang = "ru" if sys_locale.startswith("ru") else "en"

        self.i18n = {
            "ru": {
                "window_title": "Мост NeuroTrack ↔ Trackduino",
                "hero_title": "Нейроинтерфейс Роботрек",
                "ports_group": "Порты устройств",
                "state_group": "Состояние",
                "content_group": "График и индикаторы",
                "neuro_label": "🧠 Нейротрек:",
                "track_label": "🤖 Трекдуино:",
                "auto_connect": "Автоподключение при доступности",
                "btn_neuro_connect": "Подключить NeuroTrack",
                "btn_neuro_disconnect": "Отключить NeuroTrack",
                "btn_track_connect": "Подключить Trackduino",
                "btn_track_disconnect": "Отключить Trackduino",
                "btn_refresh": "Обновить порты",
                "api_access": "API",
                "footer_ports": "🧠 Нейротрек: {neuro}   |   🤖 Трекдуино: {track}",
                "status_waiting": "Ожидание подключения…",
                "status_prefix": "состояние нейротрек",
                "api_info_title": "API",
                "api_info_text": "API эндпоинт: http://127.0.0.1:8765/stream/data\n(Только чтение, без прямого доступа).",
                "attention": "Концентрация",
                "meditation": "Медитация",
                "attention_value": "Концентрация: {value}%",
                "meditation_value": "Медитация: {value}%",
                "plot_level": "Уровень",
                "plot_time": "Время",
                "plot_seconds": "сек",
            },
            "en": {
                "window_title": "NeuroTrack ↔ Trackduino Bridge",
                "hero_title": "Robotrack neural interface",
                "ports_group": "Device Ports",
                "state_group": "Status",
                "content_group": "Chart and Indicators",
                "neuro_label": "🧠 NeuroTrack:",
                "track_label": "🤖 Trackduino:",
                "auto_connect": "Auto-connect when available",
                "btn_neuro_connect": "Connect NeuroTrack",
                "btn_neuro_disconnect": "Disconnect NeuroTrack",
                "btn_track_connect": "Connect Trackduino",
                "btn_track_disconnect": "Disconnect Trackduino",
                "btn_refresh": "Update Ports",
                "api_access": "API",
                "footer_ports": "🧠 NeuroTrack: {neuro}   |   🤖 Trackduino: {track}",
                "status_waiting": "Waiting for connection…",
                "status_prefix": "NeuroTrack status",
                "api_info_title": "API",
                "api_info_text": "API endpoint: http://127.0.0.1:8765/stream/data\n(Read-only, no direct access required).",
                "attention": "Concentration",
                "meditation": "Meditation",
                "attention_value": "Concentration: {value}%",
                "meditation_value": "Meditation: {value}%",
                "plot_level": "Level",
                "plot_time": "Time",
                "plot_seconds": "sec",
            },
        }

        self.root.title(self.i18n[self.current_lang]["window_title"])
        self.root.geometry("1200x800")
        self.root.configure(bg='#070b16')

        # Настройка стилей
        self.setup_styles()

        # Очередь для коммуникации между потоками и GUI
        self.callback_queue = queue.Queue()

        # Переменные состояния
        self.neuro_connected = False
        self.track_connected = False
        self.last_neuro_state = ""
        self.last_track_state = ""
        self.last_toast_ts = 0.0
        self.last_neuro_attempt_ts = 0.0
        self.last_selected_track_port = None
        self.neuro_retry_delay = 5.0
        self.status_hold_until = 0.0

        # Данные для графиков
        self.t0 = None
        self.max_points = 300
        self.x_data = []
        self.a_data = []
        self.m_data = []

        self.cur_a = 0
        self.cur_m = 0
        self.cur_b = 0
        self.cur_poor = 200
        self.has_live_sample = False

        # Потоки
        self.reader = None
        self.bridge = None

        # Создание интерфейса
        self.create_widgets()

        # Загрузка портов
        self.refresh_ports()

        # Запуск HTTP-сервера
        self.local_server = LocalhostDataServer(self._collect_export_payload, "127.0.0.1", 8765)
        self.local_server.start()

        # Запуск обработки очереди
        self.process_callbacks()

        # Таймеры
        self.root.after(200, self.on_periodic)
        self.root.after(10000, self.on_ports_timer)
        self.root.after(4000, self.try_autoconnect)

        # Обработка закрытия окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_styles(self):
        style = ttk.Style()
        style.theme_use('clam')

        # Цветовая схема
        bg_color = '#070b16'
        fg_color = '#eaf2ff'
        select_color = '#365efc'

        style.configure('.', background=bg_color, foreground=fg_color, fieldbackground=bg_color)
        style.configure('TLabel', background=bg_color, foreground=fg_color)
        style.configure('TFrame', background=bg_color)
        style.configure('TLabelframe', background=bg_color, foreground=fg_color, borderwidth=1, relief='solid')
        style.configure('TLabelframe.Label', background=bg_color, foreground=fg_color)

        style.configure('TButton', background='#365efc', foreground=fg_color, borderwidth=1,
                        focusthickness=3, focuscolor='none')
        style.map('TButton',
                  background=[('active', '#4872ff'), ('pressed', '#2a4fd8')])

        style.configure('Small.TButton', padding=2)

        style.configure('TCombobox', fieldbackground='#0c152a', foreground=fg_color,
                        arrowcolor=fg_color, bordercolor='#2c4479')

        style.configure('TCheckbutton', background=bg_color, foreground=fg_color)

    def create_widgets(self):
        # Основной контейнер
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Заголовок
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        title_label = ttk.Label(header_frame, text=self.i18n[self.current_lang]["hero_title"],
                                font=('Arial', 16, 'bold'))
        title_label.pack(side=tk.LEFT)

        # Кнопки языка и API
        header_buttons = ttk.Frame(header_frame)
        header_buttons.pack(side=tk.RIGHT)

        self.btn_lang_ru = ttk.Button(header_buttons, text="Ru", style='Small.TButton',
                                      command=lambda: self.set_language("ru"))
        self.btn_lang_ru.pack(side=tk.LEFT, padx=2)

        self.btn_lang_en = ttk.Button(header_buttons, text="En", style='Small.TButton',
                                      command=lambda: self.set_language("en"))
        self.btn_lang_en.pack(side=tk.LEFT, padx=2)

        self.btn_api = ttk.Button(header_buttons, text=self.i18n[self.current_lang]["api_access"],
                                 style='Small.TButton', command=self.open_api_access)
        self.btn_api.pack(side=tk.LEFT, padx=(10, 0))

        # Блок портов
        self.ports_frame = ttk.LabelFrame(main_frame, text=self.i18n[self.current_lang]["ports_group"], padding=10)
        self.ports_frame.pack(fill=tk.X, pady=(0, 10))

        # NeuroTrack
        ttk.Label(self.ports_frame, text=self.i18n[self.current_lang]["neuro_label"]).grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)

        self.neuro_combo = ttk.Combobox(self.ports_frame, state='readonly', width=40)
        self.neuro_combo.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        self.btn_neuro_toggle = ttk.Button(self.ports_frame, text=self.i18n[self.current_lang]["btn_neuro_connect"],
                                          command=self.on_toggle_neuro)
        self.btn_neuro_toggle.grid(row=0, column=2, padx=5, pady=5)

        # Trackduino
        ttk.Label(self.ports_frame, text=self.i18n[self.current_lang]["track_label"]).grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)

        self.track_combo = ttk.Combobox(self.ports_frame, state='readonly', width=40)
        self.track_combo.grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        self.btn_track_toggle = ttk.Button(self.ports_frame, text=self.i18n[self.current_lang]["btn_track_connect"],
                                          command=self.on_toggle_track)
        self.btn_track_toggle.grid(row=1, column=2, padx=5, pady=5)

        # Кнопка обновления
        self.btn_refresh = ttk.Button(self.ports_frame, text=self.i18n[self.current_lang]["btn_refresh"],
                                     command=self.refresh_ports)
        self.btn_refresh.grid(row=0, column=3, rowspan=2, padx=5, pady=5, sticky=tk.NS)

        # Автоподключение
        self.auto_var = tk.BooleanVar(value=False)
        self.auto_check = ttk.Checkbutton(self.ports_frame, text=self.i18n[self.current_lang]["auto_connect"],
                                         variable=self.auto_var)
        self.auto_check.grid(row=2, column=0, columnspan=4, sticky=tk.W, padx=5, pady=5)

        # Блок состояния
        self.status_frame = ttk.LabelFrame(main_frame, text=self.i18n[self.current_lang]["state_group"], padding=10)
        self.status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_label = ttk.Label(self.status_frame, text=self.i18n[self.current_lang]["status_waiting"],
                                      font=('Arial', 10, 'bold'))
        self.status_label.pack()

        # Блок с графиком и индикаторами
        self.content_frame = ttk.LabelFrame(main_frame, text=self.i18n[self.current_lang]["content_group"], padding=10)
        self.content_frame.pack(fill=tk.BOTH, expand=True)

        # Создаем горизонтальный контейнер
        content_hbox = ttk.Frame(self.content_frame)
        content_hbox.pack(fill=tk.BOTH, expand=True)

        # Вертикальные индикаторы
        bars_frame = ttk.Frame(content_hbox, width=200)
        bars_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        bars_frame.pack_propagate(False)

        self.vbar_a = VerticalBar(bars_frame, self.i18n[self.current_lang]["attention"], "#00ff99")
        self.vbar_a.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        self.vbar_m = VerticalBar(bars_frame, self.i18n[self.current_lang]["meditation"], "#00bfff")
        self.vbar_m.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        # График
        plot_frame = ttk.Frame(content_hbox)
        plot_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.setup_plot(plot_frame)

        # Нижняя панель
        self.footer_label = ttk.Label(main_frame, text="", style='TLabel')
        self.footer_label.pack(fill=tk.X, pady=(10, 0))

        self.update_footer()

    def setup_plot(self, parent):
        # Создаем фигуру matplotlib
        self.fig = Figure(figsize=(8, 4), dpi=100, facecolor='#091126')
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('#091126')
        self.ax.tick_params(colors='#98a7cc')
        self.ax.spines['bottom'].set_color('#2a3652')
        self.ax.spines['top'].set_color('#2a3652')
        self.ax.spines['left'].set_color('#2a3652')
        self.ax.spines['right'].set_color('#2a3652')
        self.ax.xaxis.label.set_color('#98a7cc')
        self.ax.yaxis.label.set_color('#98a7cc')
        self.ax.set_ylim(0, 100)

        # Линии
        self.line_a, = self.ax.plot([], [], color='#00ff99', linewidth=2, label=self.i18n[self.current_lang]["attention"])
        self.line_m, = self.ax.plot([], [], color='#00bfff', linewidth=2, label=self.i18n[self.current_lang]["meditation"])

        self.ax.legend(facecolor='#0f1320', labelcolor='#98a7cc')

        # Встраиваем в Tkinter
        self.canvas = FigureCanvasTkAgg(self.fig, parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def update_plot(self):
        if self.x_data:
            self.line_a.set_data(self.x_data, self.a_data)
            self.line_m.set_data(self.x_data, self.m_data)

            # Автомасштабирование по X
            if len(self.x_data) > 1:
                self.ax.set_xlim(self.x_data[0], self.x_data[-1])

            self.canvas.draw_idle()

    # ---------- утилиты ----------
    def _toast(self, title: str, text: str):
        now = time.time()
        if now - self.last_toast_ts < 3:
            return
        self.last_toast_ts = now
        Toast(self.root, title, text, timeout_ms=20000)

    def _collect_export_payload(self) -> Dict[str, object]:
        return {
            "n": {
                "a": int(self.cur_a),
                "m": int(self.cur_m),
                "b": int(self.cur_b),
            }
        }

    def update_footer(self):
        neuro = self.neuro_combo.get().split(' — ')[0] if self.neuro_connected and self.neuro_combo.get() else "—"
        track = self.track_combo.get().split(' — ')[0] if self.track_connected and self.track_combo.get() else "—"
        self.footer_label.config(text=self.i18n[self.current_lang]["footer_ports"].format(neuro=neuro, track=track))

    # ---------- обработка очереди ----------
    def process_callbacks(self):
        try:
            while True:
                callback = self.callback_queue.get_nowait()
                self.handle_callback(callback)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_callbacks)

    def handle_callback(self, callback):
        if callback[0] == "neuro_status":
            self.on_neuro_status(callback[1])
        elif callback[0] == "neuro_sample":
            _, a, m, b, poor = callback
            self.on_sample(a, m, b, poor)
        elif callback[0] == "track_status":
            self.on_track_status(callback[1])
        elif callback[0] == "track_opened":
            _, ok, msg = callback
            self.on_track_opened(ok, msg)

    # ---------- порты ----------
    def refresh_ports(self):
        prev_neuro = self.neuro_combo.get()
        prev_track = self.track_combo.get()

        ports = list_com_ports()

        neuro_items = []
        track_items = []
        neuro_index = -1
        track_index = -1

        for i, (dev, name) in enumerate(ports):
            neuro_label = f"{dev} — {name}"
            track_label = f"{dev} — {self._track_port_display_name(name)}"

            neuro_items.append(neuro_label)
            track_items.append(track_label)

            if neuro_label == prev_neuro:
                neuro_index = i
            if track_label == prev_track:
                track_index = i

        self.neuro_combo['values'] = neuro_items
        self.track_combo['values'] = track_items

        if neuro_index >= 0:
            self.neuro_combo.current(neuro_index)
        elif neuro_items:
            self.neuro_combo.current(0)

        if track_index >= 0:
            self.track_combo.current(track_index)

    def on_ports_timer(self):
        self.refresh_ports()
        self.root.after(10000, self.on_ports_timer)

    @staticmethod
    def _is_trackduino_usb_port(port_name: str) -> bool:
        normalized = (port_name or "").casefold()
        markers = (
            "trackduino",
            "robotrack",
            "arduino",
            "usb serial",
            "usb-serial",
            "ch340",
            "cp210",
            "ftdi",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _track_port_display_name(port_name: str) -> str:
        normalized = (port_name or "").casefold()
        if "usb serial" in normalized or "usb-serial" in normalized or "trackduino" in normalized:
            return "Trackduino"
        return port_name

    # ---------- подключение / отключение ----------
    def on_toggle_neuro(self):
        if self.neuro_connected:
            self.disconnect_neuro()
        else:
            self.connect_neuro()

    def on_toggle_track(self):
        if self.track_connected:
            self.disconnect_track()
        else:
            self.connect_track()

    def sync_buttons(self):
        t = self.i18n[self.current_lang]
        self.btn_neuro_toggle.config(text=t["btn_neuro_disconnect"] if self.neuro_connected else t["btn_neuro_connect"])
        self.btn_track_toggle.config(text=t["btn_track_disconnect"] if self.track_connected else t["btn_track_connect"])

    def connect_neuro(self):
        now = time.time()
        if now - self.last_neuro_attempt_ts < self.neuro_retry_delay:
            return
        self.last_neuro_attempt_ts = now

        if not self.neuro_combo.get():
            messagebox.showwarning("NeuroTrack", "Не выбран порт NeuroTrack.")
            return

        if self.neuro_connected:
            return

        self.t0 = None
        self.has_live_sample = False
        self.x_data.clear()
        self.a_data.clear()
        self.m_data.clear()

        self.cur_a = self.cur_m = self.cur_b = 0
        self.cur_poor = 200

        port = self.neuro_combo.get().split(' — ')[0]

        self.reader = NeuroReader(port, self.callback_queue)
        self.reader.start()

        self.root.after(4000, self.verify_neuro_connection)

        self.update_footer()
        self.sync_buttons()

    def verify_neuro_connection(self):
        if not self.reader or not self.reader.is_alive():
            self.neuro_connected = False
            self.reader = None
            self.sync_buttons()
            return

        if not self.has_live_sample:
            if self.reader:
                self.reader.stop()
                self.reader.join(timeout=1)
            self.reader = None
            self.neuro_connected = False
            self.sync_buttons()
            return

        self.neuro_connected = True
        self.sync_buttons()
        self.update_footer()

    def disconnect_neuro(self):
        self.neuro_connected = False

        if self.reader:
            self.reader.stop()
            self.reader.join(timeout=2)

        self.reader = None

        # даём Bluetooth освободить порт
        time.sleep(0.5)

        self.cur_a = self.cur_m = self.cur_b = 0
        self.cur_poor = 200

        if self.bridge and self.bridge.is_alive():
            self.bridge.set_neuro_values(0, 0, 0, False)

        self.status_label.config(text=self.i18n[self.current_lang]["status_waiting"])
        self.sync_buttons()
        self.update_footer()

    def connect_track(self):
        track = self.track_combo.get().split(' — ')[0] if self.track_combo.get() else None

        # Автоопределение
        if not track:
            items = self.track_combo['values']
            for i, item in enumerate(items):
                if self._is_trackduino_usb_port(item):
                    self.track_combo.current(i)
                    track = item.split(' — ')[0]
                    break

        if not track:
            messagebox.showwarning("Trackduino", "Не выбран порт Trackduino.")
            return

        self.last_selected_track_port = track

        if self.track_connected:
            return

        self.bridge = TrackBridge(track, self.callback_queue, 115200)
        self.bridge.start()

        self.track_connected = True
        self.sync_buttons()
        self.update_footer()

    def disconnect_track(self):
        self.track_connected = False

        if self.bridge and self.bridge.is_alive():
            self.bridge.stop()
            self.bridge.join(timeout=1.5)

        self.bridge = None
        self.sync_buttons()
        self.update_footer()

    # ---------- обработчики событий ----------
    def on_neuro_status(self, s: str):
        self.last_neuro_state = s
        shown_state = self._translate_neuro_state(s)
        now = time.time()
        critical_states = {"Отключено", "Нет сигнала", "Плохой контакт", "Нейротрек снят"}

        if now < self.status_hold_until and s not in critical_states:
            return

        self.status_label.config(text=f"{self.i18n[self.current_lang]['status_prefix']}: {shown_state}",
                                foreground=nice_state_color(shown_state))

        if s in critical_states:
            self.status_hold_until = now + 2.0
            self.cur_a = self.cur_m = self.cur_b = 0
            self.cur_poor = 200
            self.vbar_a.set_value(0)
            self.vbar_m.set_value(0)

            if self.bridge and self.bridge.is_alive():
                self.bridge.set_neuro_values(0, 0, 0, False)

            if s == "Нет сигнала" and self.neuro_connected:
                self.root.after(0, self.disconnect_neuro)

    def on_track_opened(self, ok: bool, msg: str):
        if not ok:
            self._toast("Trackduino", msg)
            self.track_connected = False
            self.bridge = None
            self.sync_buttons()
        self.update_footer()

    def on_track_status(self, s: str):
        self.last_track_state = s
        if s == "Отключено":
            self.track_connected = False
            self.bridge = None
            self.sync_buttons()
            self._toast("Trackduino",
                        "Trackduino disconnected or COM port lost." if self.current_lang == "en" else "Trackduino отключено или потерян COM-порт.")
        self.update_footer()

    def on_sample(self, a: int, m: int, b: int, poor: int):
        self.cur_a = int(max(0, min(100, a)))
        self.cur_m = int(max(0, min(100, m)))
        self.cur_b = 0  # Blink отключён
        self.cur_poor = int(max(0, min(200, poor)))

        if not self.has_live_sample:
            self.has_live_sample = True
            self.t0 = time.time()

        # Обновляем индикаторы
        self.vbar_a.set_value(self.cur_a)
        self.vbar_m.set_value(self.cur_m)

        # neuro OK: poor==0 и статус "Подключено"
        neuro_ok = (self.cur_poor == 0) and (self.last_neuro_state == "Подключено")

        if self.bridge and self.bridge.is_alive():
            self.bridge.set_neuro_values(self.cur_a, self.cur_m, self.cur_b, neuro_ok)

    def on_periodic(self):
        if self.neuro_connected and self.t0 is not None:
            t = time.time() - self.t0
            self.x_data.append(t)
            self.a_data.append(self.cur_a)
            self.m_data.append(self.cur_m)

            if len(self.x_data) > self.max_points:
                self.x_data = self.x_data[-self.max_points:]
                self.a_data = self.a_data[-self.max_points:]
                self.m_data = self.m_data[-self.max_points:]

            self.update_plot()

        self.root.after(200, self.on_periodic)

    # ---------- автоподключение ----------
    def try_autoconnect(self):
        if not self.auto_var.get():
            self.root.after(4000, self.try_autoconnect)
            return

        if self.neuro_combo.get() and not self.neuro_connected:
            self.connect_neuro()

        if not self.track_connected and self.last_selected_track_port:
            items = self.track_combo['values']
            for i, item in enumerate(items):
                if self.last_selected_track_port in item:
                    self.track_combo.current(i)
                    self.connect_track()
                    break

        self.root.after(4000, self.try_autoconnect)

    # ---------- перевод ----------
    def set_language(self, lang: str):
        if lang not in self.i18n:
            return
        self.current_lang = lang
        t = self.i18n[lang]

        self.root.title(t["window_title"])
        self.ports_frame.config(text=t["ports_group"])
        self.status_frame.config(text=t["state_group"])
        self.content_frame.config(text=t["content_group"])

        # Обновляем все тексты
        for widget in self.ports_frame.winfo_children():
            if isinstance(widget, ttk.Label) and widget.cget('text') in ['🧠 Нейротрек:', '🤖 Трекдуино:']:
                if 'Нейротрек' in widget.cget('text'):
                    widget.config(text=t["neuro_label"])
                else:
                    widget.config(text=t["track_label"])

        self.btn_neuro_toggle.config(text=t["btn_neuro_connect"] if not self.neuro_connected else t["btn_neuro_disconnect"])
        self.btn_track_toggle.config(text=t["btn_track_connect"] if not self.track_connected else t["btn_track_disconnect"])
        self.btn_refresh.config(text=t["btn_refresh"])
        self.auto_check.config(text=t["auto_connect"])
        self.btn_api.config(text=t["api_access"])

        self.status_label.config(text=t["status_waiting"])
        self.update_footer()

        # Обновить подписи на графике
        self.ax.legend([self.line_a, self.line_m], [t["attention"], t["meditation"]],
                      facecolor='#0f1320', labelcolor='#98a7cc')
        self.canvas.draw_idle()

    def _translate_neuro_state(self, s: str) -> str:
        if self.current_lang == "en":
            return {
                "Подключение...": "Connecting...",
                "Подключено": "Connected",
                "Нормальный контакт": "Normal contact",
                "Плохой контакт": "Poor contact",
                "Нейротрек снят": "NeuroTrack removed",
                "Нет сигнала": "No signal",
                "Отключено": "Disconnected",
            }.get(s, s)
        return s

    def open_api_access(self):
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765/stream")

    def on_closing(self):
        self.disconnect_neuro()
        self.disconnect_track()
        self.local_server.stop()
        self.root.destroy()


# ==============================
# --- Запуск приложения
# ==============================
def main() -> int:
    root = tk.Tk()
    app = BridgeUI(root)

    # Обработка Ctrl+C
    def sigint_handler(*args):
        root.quit()

    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, sigint_handler)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        app.local_server.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
