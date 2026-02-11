# -*- coding: utf-8 -*-
"""
Мост NeuroTrack ↔ Trackduino
Ключевые требования, реализованные в этой версии:
- кроссплатформенный вывод портов (без обязательного winreg);
- чтение NeuroTrack (TGAM EEG 2.9 / ThinkGear) + PoorSignal (0x02);
- статусы: Подключение..., Подключено, Плохой контакт, Нейротрек снят, Нет сигнала, Отключено;
- моргание приводится к диапазону 0–100;
- обмен с Trackduino строго по протоколу TrackduinoRemote:
  Trackduino -> ПК: <{"Nreq":X}>
  ПК -> Trackduino: <{"n":{"a":A,"m":M,"b":B}}>
  При потере сигнала/контакта: A=M=B=0;
- обновление списка портов не чаще 1 раза в 10 секунд и без сброса выбранных портов;
- уведомления (небольшие окна) на 20 секунд при отключении питания/потере порта/потере контакта.
"""
import sys, time, json, threading, platform, socket, signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urllib_request
from typing import List, Tuple, Optional, Dict

from PyQt5 import QtWidgets, QtCore
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
    Вертикальный прогресс-бар, который заполняется СВЕРХУ ВНИЗ.
    """
    def __init__(self, title: str):
        super().__init__()
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(True)
        self.setFormat(f"{title}\n%v%")
        self.setOrientation(QtCore.Qt.Vertical)
        # заполнять сверху вниз
        self.setInvertedAppearance(True)
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

    def run(self):
        try:
            self._emit_status("Подключение...")
            ser = serial.Serial(self.port, 57600, **serial_open_kwargs(timeout=0.1))
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
                                blink_0_100 = self._blink_to_0_100(payload[i])
                                i += 1
                                continue

                            # multi-byte values: code >= 0x80, next is length
                            if code >= 0x80 and i < len(payload):
                                ln = payload[i]
                                i += 1 + ln
                                continue

                            # single-byte unknown
                            # nothing else to do

                        self._last_data_ts = time.time()
                        if not self._has_first_packet:
                            self._has_first_packet = True

                        # статус по poor
                        if poor == 0:
                            self._emit_status("Подключено")
                        else:
                            # Требование: формулировка "Нейротрек снят"
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

        self._last_status = ""

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
        ser = None
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
                        # ожидаем JSON запроса
                        try:
                            obj = json.loads(frame.decode("utf-8", errors="ignore"))
                        except Exception:
                            continue

                        if isinstance(obj, dict) and "Nreq" in obj:
                            self._last_req_ts = time.time()
                            # отвечаем всегда полным пакетом "n"
                            ser.write(self._build_reply())
                            ser.flush()

                # если давно не приходило запросов – это не ошибка, просто молчим
                # но если порт отвалился – упадём в exception выше

            except Exception:
                self.opened.emit(False, "Trackduino: отключено (потеря порта)")
                self._emit_status("Отключено")
                break

        try:
            if ser:
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
        "Отключено": "#ff3333",
        "Нет сигнала": "#ff3333",
        "Подключение...": "#ffcc00",
        "Плохой контакт": "#ffcc00",
        "Нейротрек снят": "#ffcc00",
        "Подключено": "#00cc66",
    }.get(s, "#ff3333")


class BridgeUI(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Мост NeuroTrack ↔ Trackduino")
        self.resize(1100, 760)

        self.setStyleSheet("""
            QWidget { background:#0f0f12; color:#f0f0f0; font-family:'Noto Sans','DejaVu Sans','Arial',sans-serif; font-size:11pt; }
            QGroupBox { border:1px solid #2a2a33; border-radius:10px; margin-top:12px; padding-top:10px; }
            QGroupBox::title { color:#ff9f1a; font-weight:600; left:10px; }
            QPushButton { border:none; color:#000; background-color:#ff9f1a; padding:8px 18px; border-radius:18px; font-weight:600; }
            QPushButton:hover { background-color:#ffb43c; }
            QComboBox { padding:6px; border-radius:10px; background:#18191e; border:1px solid #2a2a33; }
            QLabel { color:#f0f0f0; }
            QSpinBox { padding:4px; border-radius:10px; background:#18191e; border:1px solid #2a2a33; }
        """)

        root = QtWidgets.QVBoxLayout(self)

        # --- Ports box ---
        ports_box = QtWidgets.QGroupBox("Порты устройств")
        grid = QtWidgets.QGridLayout(ports_box)

        self.cb_neuro = QtWidgets.QComboBox()
        self.cb_track = QtWidgets.QComboBox()

        grid.addWidget(QtWidgets.QLabel("🧠 NeuroTrack:"), 0, 0)
        grid.addWidget(self.cb_neuro, 0, 1)

        grid.addWidget(QtWidgets.QLabel("🤖 Trackduino:"), 1, 0)
        grid.addWidget(self.cb_track, 1, 1)

        self.btn_refresh = QtWidgets.QPushButton("Обновить порты")
        self.btn_refresh.clicked.connect(self.refresh_ports)
        grid.addWidget(self.btn_refresh, 0, 2, 2, 1)

        self.auto_cb = QtWidgets.QCheckBox("Автоподключение при доступности")
        grid.addWidget(self.auto_cb, 2, 0, 1, 3)

        root.addWidget(ports_box)

        # --- State box ---
        state_box = QtWidgets.QGroupBox("Состояние")
        h = QtWidgets.QHBoxLayout(state_box)
        self.status_label = QtWidgets.QLabel("Ожидание подключения…")
        self.status_label.setStyleSheet("font-weight:600;")
        self.btn_connect = QtWidgets.QPushButton("Подключить")
        self.btn_connect.clicked.connect(self.on_toggle)
        h.addWidget(self.status_label, 1)
        h.addWidget(self.btn_connect, 0)
        root.addWidget(state_box)

        # --- Top indicators (two columns) + blink ---
        gauges_box = QtWidgets.QGroupBox("Текущие значения")
        g = QtWidgets.QGridLayout(gauges_box)

        self.vbar_a = VerticalBar("Концентрация")
        self.vbar_m = VerticalBar("Медитация")

        # раскраска "chunk" отдельно, чтобы визуально отличались
        self.vbar_a.setStyleSheet(self.vbar_a.styleSheet() + "QProgressBar::chunk { background:#00ff99; }")
        self.vbar_m.setStyleSheet(self.vbar_m.styleSheet() + "QProgressBar::chunk { background:#00bfff; }")

        self.lbl_b = QtWidgets.QLabel("Моргание: 0%")
        self.lbl_b.setStyleSheet("font-size:13pt; font-weight:600;")
        self.lbl_ps = QtWidgets.QLabel("Контакт: —")
        self.lbl_ps.setStyleSheet("color:#cfcfcf;")

        g.addWidget(self.vbar_a, 0, 0, 3, 1)
        g.addWidget(self.vbar_m, 0, 1, 3, 1)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(self.lbl_b)
        right.addWidget(self.lbl_ps)
        right.addStretch(1)

        # Порог (небольшая функция по ТЗ)
        thr_row = QtWidgets.QHBoxLayout()
        self.cb_thr = QtWidgets.QCheckBox("Порог концентрации")
        self.sp_thr = QtWidgets.QSpinBox()
        self.sp_thr.setRange(0, 100)
        self.sp_thr.setValue(60)
        thr_row.addWidget(self.cb_thr)
        thr_row.addWidget(self.sp_thr)
        right.addLayout(thr_row)

        g.addLayout(right, 0, 2, 3, 1)

        root.addWidget(gauges_box)

        # --- Plot ---
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#050506")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()
        self.plot.setYRange(0, 100)
        self.plot.setLimits(yMin=0, yMax=100)
        self.plot.setMouseEnabled(x=False, y=True)
        self.plot.setLabel("left", "Уровень", units="%")
        self.plot.setLabel("bottom", "Время", units="сек")

        self.curve_a = self.plot.plot(pen=pg.mkPen("#00ff99", width=2), name="Концентрация")
        self.curve_m = self.plot.plot(pen=pg.mkPen("#00bfff", width=2), name="Медитация")
        self.curve_b = self.plot.plot(pen=pg.mkPen("#ff6666", width=1.5), name="Моргание")

        root.addWidget(self.plot, 1)

        # --- Footer ---
        self.lbl_ports = QtWidgets.QLabel("🧠 NeuroTrack: —   |   🤖 Trackduino: —")
        root.addWidget(self.lbl_ports)

        # === internal state ===
        self.t0: Optional[float] = None
        self.max_points = 400

        self.x: List[float] = []
        self.a_hist: List[int] = []
        self.m_hist: List[int] = []
        self.b_hist: List[int] = []

        self.cur_a = 0
        self.cur_m = 0
        self.cur_b = 0
        self.cur_poor = 200

        self._connected = False
        self.reader: Optional[NeuroReader] = None
        self.bridge: Optional[TrackBridge] = None

        self._last_neuro_state = ""
        self._last_track_state = ""
        self._last_toast_ts = 0.0

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
        self.autoconn_timer.start(2000)

    # ---------- notifications ----------
    def _toast(self, title: str, text: str):
        now = time.time()
        # защита от спама: не чаще 1 раза в 3 секунды
        if now - self._last_toast_ts < 3:
            return
        self._last_toast_ts = now
        t = Toast(self, title, text, timeout_ms=20000)
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
            label = f"{dev} — {name}"
            self.cb_neuro.addItem(label, dev)
            self.cb_track.addItem(label, dev)
            if dev == prev_neuro:
                neuro_index = i
            if dev == prev_track:
                track_index = i

        if neuro_index >= 0:
            self.cb_neuro.setCurrentIndex(neuro_index)
        if track_index >= 0:
            self.cb_track.setCurrentIndex(track_index)

        self.cb_neuro.blockSignals(False)
        self.cb_track.blockSignals(False)

    def on_ports_timer(self):
        # обновляем раз в 10 секунд, но стараемся не мешать пользователю
        self.refresh_ports()

    # ---------- connect / disconnect ----------
    def on_toggle(self):
        if self._connected:
            self._disconnect_all()
        else:
            self._connect_all()

    def _connect_all(self):
        neuro = self.cb_neuro.currentData()
        track = self.cb_track.currentData()

        if not neuro:
            QtWidgets.QMessageBox.warning(self, "NeuroTrack", "Не выбран порт NeuroTrack.")
            return

        self.t0 = time.time()
        self.x.clear(); self.a_hist.clear(); self.m_hist.clear(); self.b_hist.clear()

        self.cur_a = self.cur_m = self.cur_b = 0
        self.cur_poor = 200

        # Neuro
        self.reader = NeuroReader(neuro)
        self.reader.sample.connect(self.on_sample)
        self.reader.status.connect(self.on_neuro_status)
        self.reader.start()

        # Trackduino bridge (если выбран)
        if track:
            self.bridge = TrackBridge(track, 115200)
            self.bridge.opened.connect(self.on_track_opened)
            self.bridge.status.connect(self.on_track_status)
            self.bridge.start()

        self._connected = True
        self.btn_connect.setText("Отключить")
        self.lbl_ports.setText(f"🧠 NeuroTrack: {neuro}   |   🤖 Trackduino: {track or '—'}")

    def _disconnect_all(self):
        self._connected = False

        try:
            if self.reader and self.reader.isRunning():
                self.reader.stop()
                self.reader.wait(1500)
        except Exception:
            pass

        try:
            if self.bridge and self.bridge.isRunning():
                self.bridge.stop()
                self.bridge.wait(1500)
        except Exception:
            pass

        self.reader = None
        self.bridge = None

        self.btn_connect.setText("Подключить")
        self.status_label.setText("Ожидание подключения…")

    def closeEvent(self, event):
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
    def on_neuro_status(self, s: str):
        self._last_neuro_state = s
        self.status_label.setText(f"NeuroTrack: {s}")
        self.status_label.setStyleSheet(f"font-weight:600; color:{nice_state_color(s)};")

        # уведомления по требованиям
        if s in ("Отключено", "Нет сигнала", "Плохой контакт", "Нейротрек снят"):
            self._toast("NeuroTrack", f"Состояние NeuroTrack: {s}.")

    def on_track_opened(self, ok: bool, msg: str):
        if not ok:
            self._toast("Trackduino", msg)
        self.lbl_ports.setText(msg)

    def on_track_status(self, s: str):
        self._last_track_state = s
        if s == "Отключено":
            self._toast("Trackduino", "Trackduino отключено или потерян COM-порт.")

    # ---------- samples ----------
    def on_sample(self, a: int, m: int, b: int, poor: int):
        self.cur_a = int(max(0, min(100, a)))
        self.cur_m = int(max(0, min(100, m)))
        self.cur_b = int(max(0, min(100, b)))
        self.cur_poor = int(max(0, min(200, poor)))

        # neuro OK: poor==0 и есть актуальные данные
        neuro_ok = (self.cur_poor == 0) and (self._last_neuro_state == "Подключено")

        # Пороговая логика (как опция): если включено и превышен порог – можно подсветить
        if self.cb_thr.isChecked():
            thr = int(self.sp_thr.value())
            if self.cur_a >= thr:
                self.vbar_a.setStyleSheet(self.vbar_a.styleSheet() + "QProgressBar{border:2px solid #ff9f1a;}")
            else:
                # возвращаем нормальную рамку
                self.vbar_a.setStyleSheet("""
                    QProgressBar { border: 1px solid #30303a; border-radius: 10px; background:#18191e; color:#f0f0f0; }
                    QProgressBar::chunk { background:#00ff99; border-radius: 10px; }
                """)
        else:
            self.vbar_a.setStyleSheet("""
                QProgressBar { border: 1px solid #30303a; border-radius: 10px; background:#18191e; color:#f0f0f0; }
                QProgressBar::chunk { background:#00ff99; border-radius: 10px; }
            """)

        # отправляем значения в TrackBridge
        if self.bridge and self.bridge.isRunning():
            self.bridge.set_neuro_values(self.cur_a, self.cur_m, self.cur_b, neuro_ok)

    # ---------- periodic UI update ----------
    def on_periodic(self):
        if not self._connected:
            return

        t = time.time() - (self.t0 or time.time())
        self.x.append(t)
        self.a_hist.append(self.cur_a)
        self.m_hist.append(self.cur_m)
        self.b_hist.append(self.cur_b)

        if len(self.x) > self.max_points:
            self.x = self.x[-self.max_points:]
            self.a_hist = self.a_hist[-self.max_points:]
            self.m_hist = self.m_hist[-self.max_points:]
            self.b_hist = self.b_hist[-self.max_points:]

        if self.x:
            self.plot.setXRange(self.x[0], self.x[-1] if self.x[-1] > 10 else 10)

        self.curve_a.setData(self.x, self.a_hist)
        self.curve_m.setData(self.x, self.m_hist)
        self.curve_b.setData(self.x, self.b_hist)

        self.vbar_a.setValue(self.cur_a)
        self.vbar_m.setValue(self.cur_m)

        self.lbl_b.setText(f"Моргание: {self.cur_b}%")
        if self.cur_poor == 0:
            self.lbl_ps.setText("Контакт: нормальный")
        else:
            self.lbl_ps.setText(f"Контакт: PoorSignal={self.cur_poor} (Нейротрек снят)")

    # ---------- autoconnect ----------
    def try_autoconnect(self):
        if not self.auto_cb.isChecked():
            return
        if self._connected:
            return

        # мягкое автоподключение: если есть выбранные порты – пытаемся
        if self.cb_neuro.count() == 0:
            return

        neuro = self.cb_neuro.currentData()
        if neuro:
            # Track может быть не выбран – это допустимо
            self._connect_all()


# ==============================
# --- Запуск приложения
# ==============================
def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=True)
    w = BridgeUI()
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
