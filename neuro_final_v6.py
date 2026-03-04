import serial
import time
import threading

PORT = "COM4"
BAUD = 57600

BLINK_THRESHOLD = 1000
BLINK_DEBOUNCE = 0.3

class NeuroParser:
    def __init__(self, port):
        self.ser = serial.Serial(port, BAUD, timeout=0.1)
        time.sleep(1)

        self.attention = 0
        self.meditation = 0
        self.poor_signal = 200
        self.raw = 0
        self.last_blink_time = 0
        self.blink_strength = 0

        self.enable_esense()
        self.enable_blink()

    def send_packet(self, payload):
        checksum = (~(sum(payload) & 0xFF)) & 0xFF
        packet = bytes([0xAA, 0xAA, len(payload)]) + bytes(payload) + bytes([checksum])
        self.ser.write(packet)

    def enable_esense(self):
        print("Включаем eSense...")
        self.send_packet([0x02, 0x00])
        time.sleep(0.5)

    def enable_blink(self):
        print("Пробуем включить Blink...")
        self.send_packet([0x16, 0x01])
        time.sleep(0.5)

    def checksum_ok(self, payload, chk):
        return ((~(sum(payload) & 0xFF)) & 0xFF) == chk

    def detect_blink_from_raw(self, value):
        now = time.time()
        if abs(value) > BLINK_THRESHOLD:
            if now - self.last_blink_time > BLINK_DEBOUNCE:
                self.last_blink_time = now
                self.blink_strength = min(100, int(abs(value) / 20))
                print("BLINK (RAW):", self.blink_strength)

    def parse(self):
        buffer = bytearray()

        while True:
            data = self.ser.read(64)
            if not data:
                continue

            buffer.extend(data)

            while len(buffer) >= 4:
                if buffer[0] != 0xAA or buffer[1] != 0xAA:
                    buffer.pop(0)
                    continue

                length = buffer[2]
                if len(buffer) < length + 4:
                    break

                payload = bytes(buffer[3:3+length])
                chk = buffer[3+length]
                del buffer[:length+4]

                if not self.checksum_ok(payload, chk):
                    continue

                i = 0
                while i < len(payload):
                    code = payload[i]
                    i += 1

                    if code == 0x02:
                        self.poor_signal = payload[i]
                        i += 1
                        print("PoorSignal:", self.poor_signal)

                    elif code == 0x04:
                        self.attention = payload[i]
                        i += 1
                        print("Attention:", self.attention)

                    elif code == 0x05:
                        self.meditation = payload[i]
                        i += 1
                        print("Meditation:", self.meditation)

                    elif code == 0x16:
                        self.blink_strength = payload[i]
                        i += 1
                        print("BLINK (chip):", self.blink_strength)

                    elif code == 0x80:
                        ln = payload[i]
                        i += 1
                        raw = int.from_bytes(payload[i:i+ln], byteorder='big', signed=True)
                        i += ln
                        self.raw = raw
                        self.detect_blink_from_raw(raw)

                    elif code >= 0x80:
                        ln = payload[i]
                        i += ln + 1


if __name__ == "__main__":
    print("Запуск NeuroTrack parser")
    parser = NeuroParser(PORT)
    parser.parse()
