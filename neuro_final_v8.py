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
import os, sys, time, json, threading, platform, socket, signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urllib_request
from typing import List, Tuple, Optional, Dict

from PyQt5 import QtWidgets, QtCore, QtGui
import pyqtgraph as pg
import serial
import serial.tools.list_ports

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
# --- UI widgets ---
# ==============================
class Toast(QtWidgets.QDialog):
    """
    Небольшое уведомление, показывается на 20 секунд и исчезает.
    """
    def __init__(self, parent: QtWidgets.QWidget, title: str, text: str, timeout_ms: int = 20000):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool | QtCore.Qt.WindowStaysOnTopHint)
        self.setModal(False)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)

        lay = QtWidgets.QVBoxLayout(self)
        lbl = QtWidgets.QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self.resize(420, 120)
        QtCore.QTimer.singleShot(timeout_ms, self.close)


class VerticalBar(QtWidgets.QProgressBar):
    """
    Вертикальный прогресс-бар, который заполняется СНИЗУ ВВЕРХ.
    """
    def __init__(self, title: str):
        super().__init__()
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(True)
        self.setFormat(f"{title}\n%v%")
        self.setOrientation(QtCore.Qt.Vertical)
        # заполнять снизу вверх
        self.setInvertedAppearance(False)
        # чтобы текст не вращался
        self.setStyleSheet("""
            QProgressBar { border: 1px solid #30303a; border-radius: 10px; background:#18191e; color:#f0f0f0; }
            QProgressBar::chunk { border-radius: 10px; }
        """)


# ==============================
# --- Поток чтения NeuroTrack ---
# ==============================
class NeuroReader(QtCore.QThread):
    """
    Читает ThinkGear пакеты и выдаёт:
    attention (0–100), meditation (0–100), blink (0–100), poor_signal (0–200)
    """
    sample = QtCore.pyqtSignal(int, int, int, int)
    status = QtCore.pyqtSignal(str)

    def __init__(self, port: str):
        super().__init__()
        self.port = port
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
            self.status.emit(s)

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
                            self._emit_status("Подключено")
                        elif poor <= 50:
                            self._emit_status("Нормальный контакт")
                        elif poor <= 150:
                            self._emit_status("Плохой контакт")
                        else:
                            self._emit_status("Нейротрек снят")

                        self.sample.emit(attention, meditation, blink_0_100, poor)

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
class TrackBridge(QtCore.QThread):
    """
    Открывает порт Trackduino и отвечает на запросы Nreq:
    Trackduino -> ПК: <{"Nreq":X}>
    ПК -> Trackduino: <{"n":{"a":A,"m":M,"b":B}}>
    """
    opened = QtCore.pyqtSignal(bool, str)
    status = QtCore.pyqtSignal(str)

    def __init__(self, port: str, baud: int = 115200):
        super().__init__()
        self.port = port
        self.baud = baud
        self._run = True
        self._lock = threading.Lock()

        self._a = 0
        self._m = 0
        self._b = 0
        self._neuro_ok = False  # poor == 0 и есть свежие данные

        self._last_req_ts = 0.0

        self._buf = bytearray()
        self._last_data_ts = time.time()
        self._timeout_sec = 5.0   # сколько секунд ждём данные
        self._last_status = ""

        self._got_first_request = False

    def stop(self):
        self._run = False

    def _emit_status(self, s: str):
        if s != self._last_status:
            self._last_status = s
            self.status.emit(s)

    @QtCore.pyqtSlot(int, int, int, bool)
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
            self.opened.emit(True, "Trackduino: подключено")
            self._emit_status("Подключено")
        except Exception as e:
            self.opened.emit(False, f"Trackduino: ошибка открытия порта ({e})")
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
                self.opened.emit(False, "Trackduino: отключено (потеря порта)")
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
# --- UI
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


class BridgeUI(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        sys_locale = QtCore.QLocale.system().name().lower()
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
                "status_prefix": "состояние  нейротрек",
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

        self.setWindowTitle("Мост NeuroTrack ↔ Trackduino")
        self.setWindowIcon(self._load_app_icon())
        self.resize(1100, 760)

        self.setStyleSheet("""
            QWidget {
                color:#eaf2ff;
                font-family:'Noto Sans','DejaVu Sans','Arial',sans-serif;
                font-size:11pt;
                background:qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #070b16, stop:0.45 #0a1225, stop:1 #111733);
            }
            QGroupBox {
                border:1px solid #24365f;
                border-radius:16px;
                margin-top:14px;
                padding:14px;
                background:rgba(12, 20, 40, 0.72);
            }
            QGroupBox::title {
                color:#8fb6ff;
                font-weight:700;
                left:14px;
                top:2px;
                padding:0 6px;
            }
            QPushButton {
                border:1px solid #395ba2;
                color:#eaf2ff;
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #365efc, stop:1 #5f8bff);
                padding:9px 20px;
                border-radius:12px;
                font-weight:700;
            }
            QPushButton:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4872ff, stop:1 #79a0ff); }
            QPushButton:pressed { background:#2a4fd8; }
            QPushButton[small="true"] {
                padding:5px 11px;
                border-radius:9px;
                font-size:9.5pt;
            }
            QComboBox, QSpinBox {
                padding:7px;
                border-radius:10px;
                background:#0c152a;
                border:1px solid #2c4479;
                color:#e9f0ff;
            }
            QCheckBox { spacing:8px; }
            QLabel#heroTitle {
                font-size:16pt;
                font-weight:800;
                color:#f5f8ff;
                background:rgba(54,94,252,0.17);
                border:1px solid #4a6dfd;
                border-radius:17px;
                padding:4px 14px;
            }
            QLabel#heroSub { color:#a8bde4; font-size:10.5pt; }
            QLabel#footerBar {
                background:rgba(10,18,36,.75);
                border:1px solid #24365f;
                border-radius:10px;
                padding:8px 12px;
            }
        """)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        hero = QtWidgets.QFrame()
        hero.setStyleSheet("QFrame { background:rgba(12,22,44,0.72); border:1px solid #2d4375; border-radius:14px; }")
        hero_l = QtWidgets.QHBoxLayout(hero)
        hero_l.setContentsMargins(14, 10, 14, 10)
        hero_txt = QtWidgets.QVBoxLayout()
        title_row = QtWidgets.QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        self.title = QtWidgets.QLabel("NeuroTrack Command Center")
        self.title.setObjectName("heroTitle")
        title_row.addWidget(self.title, 0, QtCore.Qt.AlignVCenter)
        title_row.addStretch(1)

        hero_txt.addLayout(title_row)
        hero_l.addLayout(hero_txt, 1)

        lang_wrap = QtWidgets.QWidget()
        lang_l = QtWidgets.QHBoxLayout(lang_wrap)
        lang_l.setContentsMargins(0, 0, 0, 0)
        lang_l.setSpacing(6)
        self.btn_lang_ru = QtWidgets.QPushButton("Ru")
        self.btn_lang_en = QtWidgets.QPushButton("En")
        self.btn_lang_ru.setProperty("small", True)
        self.btn_lang_en.setProperty("small", True)
        self.btn_lang_ru.clicked.connect(lambda: self.set_language("ru"))
        self.btn_lang_en.clicked.connect(lambda: self.set_language("en"))
        lang_l.addWidget(self.btn_lang_ru)
        lang_l.addWidget(self.btn_lang_en)
        hero_l.addWidget(lang_wrap, 0, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        self.btn_api_access = QtWidgets.QPushButton("API access")
        self.btn_api_access.setProperty("small", True)
        self.btn_api_access.clicked.connect(self.open_api_access)
        hero_l.addWidget(self.btn_api_access, 0, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        root.addWidget(hero)

        # --- Ports box ---
        self.ports_box = QtWidgets.QGroupBox("Порты устройств")
        grid = QtWidgets.QGridLayout(self.ports_box)

        self.cb_neuro = QtWidgets.QComboBox()
        self.cb_track = QtWidgets.QComboBox()

        self.lbl_neuro_port = QtWidgets.QLabel("🧠 NeuroTrack:")
        grid.addWidget(self.lbl_neuro_port, 0, 0)
        grid.addWidget(self.cb_neuro, 0, 1)
        self.btn_neuro_toggle = QtWidgets.QPushButton("Подключить NeuroTrack")
        self.btn_neuro_toggle.setProperty("small", True)
        self.btn_neuro_toggle.clicked.connect(self.on_toggle_neuro)
        grid.addWidget(self.btn_neuro_toggle, 0, 2)

        self.lbl_track_port = QtWidgets.QLabel("🤖 Trackduino:")
        grid.addWidget(self.lbl_track_port, 1, 0)
        grid.addWidget(self.cb_track, 1, 1)
        self.btn_track_toggle = QtWidgets.QPushButton("Подключить Trackduino")
        self.btn_track_toggle.setProperty("small", True)
        self.btn_track_toggle.clicked.connect(self.on_toggle_track)
        grid.addWidget(self.btn_track_toggle, 1, 2)

        self.btn_refresh = QtWidgets.QPushButton("Обновить порты")
        self.btn_refresh.setProperty("small", True)
        self.btn_refresh.clicked.connect(self.refresh_ports)
        grid.addWidget(self.btn_refresh, 0, 3, 2, 1)

        self.auto_cb = QtWidgets.QCheckBox("Автоподключение при доступности")
        grid.addWidget(self.auto_cb, 2, 0, 1, 4)

        root.addWidget(self.ports_box)

        # --- State box ---
        self.state_box = QtWidgets.QGroupBox("Состояние")
        h = QtWidgets.QHBoxLayout(self.state_box)
        self.status_label = QtWidgets.QLabel("Ожидание подключения…")
        self.status_label.setStyleSheet("font-weight:600;")
        h.addWidget(self.status_label, 1)
        root.addWidget(self.state_box)

        # --- Plot + right bars (two-column model) ---
        self.content_box = QtWidgets.QGroupBox("График и индикаторы")
        content_layout = QtWidgets.QHBoxLayout(self.content_box)
        content_layout.setSpacing(12)

        plot_wrap = QtWidgets.QWidget()
        plot_layout = QtWidgets.QVBoxLayout(plot_wrap)
        plot_layout.setContentsMargins(0, 0, 0, 0)

        self.vbar_a = VerticalBar("Концентрация")
        self.vbar_m = VerticalBar("Медитация")

        # раскраска "chunk" отдельно, чтобы визуально отличались
        self.vbar_a.setStyleSheet(self.vbar_a.styleSheet() + "QProgressBar::chunk { background:#00ff99; }")
        self.vbar_m.setStyleSheet(self.vbar_m.styleSheet() + "QProgressBar::chunk { background:#00bfff; }")

        self.plot = pg.PlotWidget()
        self.plot.setBackground("#091126")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.setYRange(0, 100)
        self.plot.setLimits(yMin=0, yMax=100)
        self.plot.setMouseEnabled(x=False, y=True)
        self.plot.setLabel("left", "Уровень", units="%")
        self.plot.setLabel("bottom", "Время", units="сек")
        self._setup_plot_curves()

        self.plot.setMinimumHeight(420)
        plot_layout.addWidget(self.plot, 1)

        bars_panel = QtWidgets.QFrame()
        bars_panel.setStyleSheet("QFrame{background:rgba(8,16,32,.45); border:1px solid #24365f; border-radius:12px;}")
        bars_layout = QtWidgets.QHBoxLayout(bars_panel)
        bars_layout.setContentsMargins(10, 10, 10, 10)
        bars_layout.setSpacing(10)
        bars_panel.setMinimumWidth(230)

        for bar in (self.vbar_a, self.vbar_m):
            bar.setTextVisible(False)
            bar.setMinimumHeight(260)
            bar.setMinimumWidth(86)
            bar.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

        self.lbl_a_val = QtWidgets.QLabel("Концентрация: 0%")
        self.lbl_m_val = QtWidgets.QLabel("Медитация: 0%")
        self.lbl_a_val.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_m_val.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_a_val.setStyleSheet("font-size:11pt; font-weight:800; color:#a5ffcf;")
        self.lbl_m_val.setStyleSheet("font-size:11pt; font-weight:800; color:#9bd8ff;")

        a_col = QtWidgets.QVBoxLayout()
        a_col.setSpacing(6)
        a_col.addWidget(self.vbar_a, 1, QtCore.Qt.AlignHCenter)
        a_col.addWidget(self.lbl_a_val)

        m_col = QtWidgets.QVBoxLayout()
        m_col.setSpacing(6)
        m_col.addWidget(self.vbar_m, 1, QtCore.Qt.AlignHCenter)
        m_col.addWidget(self.lbl_m_val)

        bars_layout.addLayout(a_col, 1)
        bars_layout.addLayout(m_col, 1)

        content_layout.addWidget(bars_panel, 0)
        content_layout.addWidget(plot_wrap, 1)

        root.addWidget(self.content_box, 2)

        # --- Footer ---
        self.lbl_ports = QtWidgets.QLabel("")
        self.lbl_ports.setObjectName("footerBar")
        root.addWidget(self.lbl_ports)

        # === internal state ===
        self.t0: Optional[float] = None
        self.max_points = 300  # 60 seconds at 200 ms update interval

        self.x: List[float] = []
        self.a_hist: List[int] = []
        self.m_hist: List[int] = []
        

        self.cur_a = 0
        self.cur_m = 0
        self.cur_b = 0
        self.cur_poor = 200
        self._has_live_sample = False

        self._connected = False
        self._neuro_connected = False
        self._track_connected = False
        self.reader: Optional[NeuroReader] = None
        self.bridge: Optional[TrackBridge] = None

        self._last_neuro_state = ""
        self._last_track_state = ""
        self._last_toast_ts = 0.0
        self._last_neuro_attempt_ts = 0.0
        self._last_selected_track_port = None
        self._neuro_retry_delay = 5.0  # секунд между попытками
        self._status_hold_until = 0.0

        # HTTP-экспорт текущих данных на localhost
        self.local_server = LocalhostDataServer(self._collect_export_payload, "127.0.0.1", 8765)
        self.local_server.start()

        # initial ports list
        self.refresh_ports()

        # timers
        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self.on_periodic)
        self.ui_timer.start(200)

        self.ports_timer = QtCore.QTimer(self)
        self.ports_timer.timeout.connect(self.on_ports_timer)
        self.ports_timer.start(10000)  # 10 секунд

        self.autoconn_timer = QtCore.QTimer(self)
        self.autoconn_timer.timeout.connect(self.try_autoconnect)
        self.autoconn_timer.start(4000)

        self.set_language(self.current_lang)

    # ---------- notifications ----------

    def _setup_plot_curves(self):
        t = self.i18n[self.current_lang]
        self.plot.clear()
        self.plot.addLegend()
        self.curve_a = self.plot.plot(pen=pg.mkPen("#49e6a3", width=2.6), name=t["attention"])
        self.curve_m = self.plot.plot(pen=pg.mkPen("#64beff", width=2.6), name=t["meditation"])
        
        for curve in (self.curve_a, self.curve_m):
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        x_hist = self.__dict__.get("x", [])
        a_hist = self.__dict__.get("a_hist", [])
        m_hist = self.__dict__.get("m_hist", [])
        b_hist = self.__dict__.get("b_hist", [])
        self.curve_a.setData(x_hist, a_hist)
        self.curve_m.setData(x_hist, m_hist)
        

    @staticmethod
    def _asset_path(filename: str) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    def _load_hero_logo(self) -> QtGui.QPixmap:
        logo_path = self._asset_path("нейротрек.svg")
        icon = QtGui.QIcon(logo_path)
        pix = icon.pixmap(180, 52)
        if not pix.isNull():
            return pix
        return self._fallback_robotrack_logo()

    def _load_app_icon(self) -> QtGui.QIcon:
        logo_path = self._asset_path("нейротрек.svg")
        icon = QtGui.QIcon(logo_path)
        if not icon.isNull():
            return icon
        return self._fallback_app_icon()

    @staticmethod
    def _fallback_robotrack_logo() -> QtGui.QPixmap:
        pix = QtGui.QPixmap(180, 52)
        pix.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pix)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor("#2f4e89"), 1))
        painter.setBrush(QtGui.QColor("#ffffff"))
        painter.drawRoundedRect(0, 0, 179, 51, 12, 12)
        painter.setBrush(QtGui.QColor("#111111"))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(10, 12, 28, 28)
        painter.setPen(QtGui.QPen(QtGui.QColor("#111111")))
        painter.setFont(QtGui.QFont("Arial", 12, QtGui.QFont.Bold))
        painter.drawText(QtCore.QRect(46, 6, 128, 20), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, "ROBOTRACK")
        painter.setFont(QtGui.QFont("Arial", 9, QtGui.QFont.Bold))
        painter.setPen(QtGui.QPen(QtGui.QColor("#365efc")))
        painter.drawText(QtCore.QRect(46, 26, 128, 18), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, "Neural Interface")
        painter.end()
        return pix

    @staticmethod
    def _fallback_app_icon() -> QtGui.QIcon:
        base = QtGui.QPixmap(128, 128)
        base.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(base)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor("#0f1d3d"))
        painter.drawRoundedRect(4, 4, 120, 120, 26, 26)
        painter.setBrush(QtGui.QColor("#4de39c"))
        painter.drawEllipse(20, 20, 34, 34)
        painter.setBrush(QtGui.QColor("#64beff"))
        painter.drawEllipse(74, 20, 34, 34)
        painter.setBrush(QtGui.QColor("#ff8f73"))
        painter.drawEllipse(47, 66, 34, 34)
        painter.end()
        return QtGui.QIcon(base)

    def _toast(self, title: str, text: str):
        now = time.time()
        # защита от спама: не чаще 1 раза в 3 секунды
        if now - self._last_toast_ts < 3:
            return
        self._last_toast_ts = now
        t = Toast(self, title, text, timeout_ms=2000)
        t.show()

    # ---------- ports ----------
    def refresh_ports(self):
        prev_neuro = self.cb_neuro.currentData()
        prev_track = self.cb_track.currentData()

        ports = list_com_ports()

        self.cb_neuro.blockSignals(True)
        self.cb_track.blockSignals(True)

        self.cb_neuro.clear()
        self.cb_track.clear()

        neuro_index = -1
        track_index = -1

        for i, (dev, name) in enumerate(ports):
            neuro_label = f"{dev} — {name}"
            track_label = f"{dev} — {self._track_port_display_name(name)}"
            self.cb_neuro.addItem(neuro_label, dev)
            self.cb_track.addItem(track_label, dev)
            if dev == prev_neuro:
                neuro_index = i
            if dev == prev_track:
                track_index = i

        if neuro_index >= 0:
            self.cb_neuro.setCurrentIndex(neuro_index)
        if track_index >= 0:
            self.cb_track.setCurrentIndex(track_index)
        else:
            # Не делаем "слепой" выбор первого порта Trackduino в списке.
            # Список остаётся нейтральным, а авто-детект выполняется в момент подключения.
            self.cb_track.setCurrentIndex(-1)

        self.cb_neuro.blockSignals(False)
        self.cb_track.blockSignals(False)

    def on_ports_timer(self):
        # обновляем раз в 10 секунд, но стараемся не мешать пользователю
        self.refresh_ports()

    @staticmethod
    def _is_trackduino_usb_port(port_name: str) -> bool:
        """
        Эвристика для Windows: пытаемся выбрать USB-порт Trackduino по friendly name.
        Используем безопасный набор ключевых слов, чтобы не падать, если нет точного матча.
        """
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
        if "usb serial" in normalized or "usb-serial" in normalized or "nrackduino" in normalized:
            return "Trackduino"
        return port_name

    # ---------- connect / disconnect ----------
    def on_toggle_neuro(self):
        if self._neuro_connected:
            self._disconnect_neuro()
        else:
            self._connect_neuro()

    def on_toggle_track(self):
        if self._track_connected:
            self._disconnect_track()
        else:
            self._connect_track()

    def _sync_connection_flags(self):
        self._connected = self._neuro_connected or self._track_connected
        t = self.i18n[self.current_lang]
        self.btn_neuro_toggle.setText(t["btn_neuro_disconnect"] if self._neuro_connected else t["btn_neuro_connect"])
        self.btn_track_toggle.setText(t["btn_track_disconnect"] if self._track_connected else t["btn_track_connect"])

    def set_language(self, lang: str):
        if lang not in self.i18n:
            return
        self.current_lang = lang
        t = self.i18n[lang]

        self.setWindowTitle(t["window_title"])
        self.title.setText(t["hero_title"])
        self.ports_box.setTitle(t["ports_group"])
        self.state_box.setTitle(t["state_group"])
        self.content_box.setTitle(t["content_group"])
        self.lbl_neuro_port.setText(t["neuro_label"])
        self.lbl_track_port.setText(t["track_label"])
        self.auto_cb.setText(t["auto_connect"])
        self.btn_refresh.setText(t["btn_refresh"])
        self.btn_api_access.setText(t["api_access"])
        if self.status_label.text().startswith(("Ожидание", "Waiting")):
            self.status_label.setText(t["status_waiting"])

        self.vbar_a.setFormat(f"{t['attention']}\n%v%")
        self.vbar_m.setFormat(f"{t['meditation']}\n%v%")
        self.lbl_a_val.setText(t["attention_value"].format(value=self.cur_a))
        self.lbl_m_val.setText(t["meditation_value"].format(value=self.cur_m))
        self.plot.setLabel("left", t["plot_level"], units="%")
        self.plot.setLabel("bottom", t["plot_time"], units=t["plot_seconds"])
        self._setup_plot_curves()

        self._sync_connection_flags()
        if self._last_neuro_state:
            self.on_neuro_status(self._last_neuro_state)
        elif not self._neuro_connected:
            self.status_label.setText(t["status_waiting"])
            self.status_label.setStyleSheet("font-weight:600;")
        neuro = self.cb_neuro.currentData() if self._neuro_connected else "—"
        track = self.cb_track.currentData() if self._track_connected else "—"
        track_state = self._translate_track_state(self._last_track_state) if self._track_connected else (track or "—")
        self.lbl_ports.setText(t["footer_ports"].format(neuro=neuro or "—", track=track_state))


    def open_api_access(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl("http://127.0.0.1:8765/stream"))

    def _verify_neuro_connection(self):
        if not self.reader:
            return

        # если поток уже умер — порт не открылся
        if not self.reader.isRunning():
            self._neuro_connected = False
            self.reader = None
            self._sync_connection_flags()
            return

        # если поток жив, но нет данных — считаем, что устройство не отвечает
        if not self._has_live_sample:
            try:
                self.reader.stop()
                self.reader.wait(1000)
            except Exception:
                pass

            self.reader = None
            self._neuro_connected = False
            self._sync_connection_flags()
            return

        # если пришёл первый пакет — всё хорошо
        self._neuro_connected = True
        self._sync_connection_flags()

        neuro = self.cb_neuro.currentData()
        track = self.cb_track.currentData() if self._track_connected else "—"
        t = self.i18n[self.current_lang]
        self.lbl_ports.setText(t["footer_ports"].format(neuro=neuro, track=track or "—"))

    def _connect_neuro(self):
        now = time.time()
        if now - self._last_neuro_attempt_ts < self._neuro_retry_delay:
            return
        self._last_neuro_attempt_ts = now
        neuro = self.cb_neuro.currentData()
        if not neuro:
            QtWidgets.QMessageBox.warning(self, "NeuroTrack", "Не выбран порт NeuroTrack.")
            return

        if self._neuro_connected:
            return
        
        self.t0 = None

        self._has_live_sample = False
        self.x.clear(); self.a_hist.clear(); self.m_hist.clear()

        self.cur_a = self.cur_m = self.cur_b = 0
        self.cur_poor = 200

        # Neuro
        self.reader = NeuroReader(neuro)
        self.reader.sample.connect(self.on_sample)
        self.reader.status.connect(self.on_neuro_status)
        self.reader.start()

        # даём потоку 700 мс на попытку открытия порта
        QtCore.QTimer.singleShot(2000, self._verify_neuro_connection)
        self._sync_connection_flags()
        track = self.cb_track.currentData() if self._track_connected else "—"
        t = self.i18n[self.current_lang]
        self.lbl_ports.setText(t["footer_ports"].format(neuro=neuro, track=track or "—"))

    def _disconnect_neuro(self):
        self._neuro_connected = False

        try:
            if self.reader:
                self.reader.stop()
                self.reader.wait(2000)   # ждём завершения потока
        except Exception:
            pass

        self.reader = None

        # 🔥 ВАЖНО: даём Bluetooth освободить порт
        QtCore.QThread.msleep(500)

        self.cur_a = self.cur_m = self.cur_b = 0
        self.cur_poor = 200

        if self.bridge and self.bridge.isRunning():
            self.bridge.set_neuro_values(0, 0, 0, False)

        self.status_label.setText(self.i18n[self.current_lang]["status_waiting"])
        self._sync_connection_flags()


    def _connect_track(self):
        track = self.cb_track.currentData()

    # Если порт не выбран — пробуем автоопределение
        if not track:
            autodetect_index = self._find_trackduino_port_index()
            if autodetect_index >= 0:
                self.cb_track.setCurrentIndex(autodetect_index)
                track = self.cb_track.currentData()

        if not track:
            QtWidgets.QMessageBox.warning(self, "Trackduino", "Не выбран порт Trackduino.")
            return

        # 🔥 ВАЖНО: всегда запоминаем выбранный порт
        self._last_selected_track_port = track

        if self._track_connected:
            return

        self.bridge = TrackBridge(track, 115200)
        self.bridge.opened.connect(self.on_track_opened)
        self.bridge.status.connect(self.on_track_status)
        self.bridge.start()

        self._track_connected = True
        self._sync_connection_flags()

    def _disconnect_track(self):
        self._track_connected = False

        try:
            if self.bridge and self.bridge.isRunning():
                self.bridge.stop()
                self.bridge.wait(1500)
        except Exception:
            pass

        self.bridge = None
        self._sync_connection_flags()

    def _disconnect_all(self):
        self._disconnect_neuro()
        self._disconnect_track()

        t = self.i18n[self.current_lang]
        self.lbl_ports.setText(t["footer_ports"].format(neuro="—", track="—"))

    def closeEvent(self, event):
        self._disconnect_all()
        try:
            self.local_server.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def _collect_export_payload(self) -> Dict[str, object]:
        # Формат строго по требованию интеграции:
        # <{"n":{"a":A,"m":M,"b":B}}>
        return {
            "n": {
                "a": int(self.cur_a),
                "m": int(self.cur_m),
                "b": int(self.cur_b),
            }
        }


    # ---------- status handlers ----------
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

    def _translate_track_state(self, s: str) -> str:
        if self.current_lang == "en":
            return {
                "Подключено": "Connected",
                "Отключено": "Disconnected",
            }.get(s, s)
        return s

    def _status_prefix(self) -> str:
        return self.i18n[self.current_lang].get("status_prefix", "NeuroTrack")

    def on_neuro_status(self, s: str):
        self._last_neuro_state = s
        shown_state = self._translate_neuro_state(s)
        now = time.time()
        critical_states = {"Отключено", "Нет сигнала", "Плохой контакт", "Нейротрек снят или плохой контакт"}

        if now < self._status_hold_until and s not in critical_states:
            return

        self.status_label.setText(f"{self._status_prefix()}: {shown_state}")
        self.status_label.setStyleSheet(f"font-weight:600; color:{nice_state_color(shown_state)};")

        if s in critical_states:
            self._status_hold_until = now + 2.0
            self.cur_a = self.cur_m = self.cur_b = 0
            self.cur_poor = 200
            self.vbar_a.setValue(0)
            self.vbar_m.setValue(0)
            t = self.i18n[self.current_lang]
            self.lbl_a_val.setText(t["attention_value"].format(value=0))
            self.lbl_m_val.setText(t["meditation_value"].format(value=0))
            if self.bridge and self.bridge.isRunning():
                self.bridge.set_neuro_values(0, 0, 0, False)
            
             # если нет сигнала — считаем устройство отключённым
            if s == "Нет сигнала" and self._neuro_connected:
                QtCore.QTimer.singleShot(0, self._disconnect_neuro)

    def on_track_opened(self, ok: bool, msg: str):
        if not ok:
            self._toast("Trackduino", msg)
            self._track_connected = False
            self.bridge = None
            self._sync_connection_flags()
        t = self.i18n[self.current_lang]
        neuro = self.cb_neuro.currentData() if self._neuro_connected else "—"
        track_state = self._translate_track_state("Подключено" if ok else "Отключено")
        self.lbl_ports.setText(t["footer_ports"].format(neuro=neuro or "—", track=track_state))

    def on_track_status(self, s: str):
        self._last_track_state = s
        if s == "Отключено":
            self._track_connected = False
            self.bridge = None
            self._sync_connection_flags()
            self._toast("Trackduino", "Trackduino disconnected or COM port lost." if self.current_lang == "en" else "Trackduino отключено или потерян COM-порт.")

    # ---------- samples ----------
    def on_sample(self, a: int, m: int, b: int, poor: int):
        self.cur_a = int(max(0, min(100, a)))
        self.cur_m = int(max(0, min(100, m)))

        # 🔥 Blink отключён
        self.cur_b = 0

        self.cur_poor = int(max(0, min(200, poor)))

        if not self._has_live_sample:
            self._has_live_sample = True
            self.t0 = time.time()

        # Обновляем индикаторы сразу при приходе данных (без ожидания on_periodic).
        self.vbar_a.setValue(self.cur_a)
        self.vbar_m.setValue(self.cur_m)
        t = self.i18n[self.current_lang]
        self.lbl_a_val.setText(t["attention_value"].format(value=self.cur_a))
        self.lbl_m_val.setText(t["meditation_value"].format(value=self.cur_m))

        # neuro OK: poor==0 и есть актуальные данные
        neuro_ok = (self.cur_poor == 0) and (self._last_neuro_state == "Подключено")

        # отправляем значения в TrackBridge
        if self.bridge and self.bridge.isRunning():
            self.bridge.set_neuro_values(self.cur_a, self.cur_m, self.cur_b, neuro_ok)

    # ---------- periodic UI update ----------
    def on_periodic(self):
        # Рисуем график, пока подключен NeuroTrack: даже если пакетов ещё нет,
        # пользователь видит «живую» временную шкалу и текущие значения (обычно 0).
        if not self._neuro_connected:
            return

        if self.t0 is None:
            self.t0 = time.time()

        t = time.time() - self.t0
        self.x.append(t)
        self.a_hist.append(self.cur_a)
        self.m_hist.append(self.cur_m)
        

        if len(self.x) > self.max_points:
            self.x = self.x[-self.max_points:]
            self.a_hist = self.a_hist[-self.max_points:]
            self.m_hist = self.m_hist[-self.max_points:]
            

        if self.x:
            self.plot.setXRange(self.x[0], self.x[-1] if self.x[-1] > 10 else 10)

        x = getattr(self, "x", [])
        a_hist = getattr(self, "a_hist", [])
        m_hist = getattr(self, "m_hist", [])
        b_hist = getattr(self, "b_hist", [])
        self.curve_a.setData(x, a_hist)
        self.curve_m.setData(x, m_hist)
        

        self.vbar_a.setValue(self.cur_a)
        self.vbar_m.setValue(self.cur_m)
        t = self.i18n[self.current_lang]
        self.lbl_a_val.setText(t["attention_value"].format(value=self.cur_a))
        self.lbl_m_val.setText(t["meditation_value"].format(value=self.cur_m))


    # ---------- autoconnect ----------
    def try_autoconnect(self):
        if not self.auto_cb.isChecked():
            return

        # мягкое автоподключение: если есть выбранные порты – пытаемся
        if self.cb_neuro.count() > 0 and not self._neuro_connected:
            neuro = self.cb_neuro.currentData()
            if neuro:
                self._connect_neuro()

        if not self._track_connected and self._last_selected_track_port:
            for i in range(self.cb_track.count()):
                if self.cb_track.itemData(i) == self._last_selected_track_port:
                    self.cb_track.setCurrentIndex(i)
                    self._connect_track()
                    break

    def _find_trackduino_port_index(self) -> int:
        for i in range(self.cb_track.count()):
            name = self.cb_track.itemText(i)
            if self._is_trackduino_usb_port(name):
                return i
        return -1


# ==============================
# --- Запуск приложения
# ==============================
def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=False)
    w = BridgeUI()
    app.setWindowIcon(w.windowIcon())
    w.show()

    # Позволяет корректно завершать Qt-приложение по Ctrl+C без traceback.
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, lambda *_: app.quit())

    # Пустой таймер, чтобы Python регулярно обрабатывал сигналы,
    # пока event loop Qt активен.
    sig_timer = QtCore.QTimer()
    sig_timer.start(200)
    sig_timer.timeout.connect(lambda: None)

    try:
        return int(app.exec_())
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            w.local_server.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())