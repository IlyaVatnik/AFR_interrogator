# -*- coding: utf-8 -*-
"""
Created on Tue Oct 28 11:31:22 2025

@author: Илья
"""

import serial
import time
from typing import Optional, Dict, Any

__version__='1.0'
__date__='28.10.2025'


def ip_to_bytes(ip: str) -> bytes:
    parts = [int(x) for x in ip.strip().split(".")]
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        raise ValueError("Некорректный IPv4")
    return bytes(parts)


def mac_to_bytes(mac: str) -> bytes:
    mac = mac.strip().replace("-", ":")
    parts = [int(x, 16) for x in mac.split(":")]
    if len(parts) != 6 or any(p < 0 or p > 255 for p in parts):
        raise ValueError("Некорректный MAC")
    return bytes(parts)


def bytes_to_ip(b: bytes) -> str:
    if len(b) != 4:
        raise ValueError("Ожидалось 4 байта для IP")
    return ".".join(str(x) for x in b)


def bytes_to_mac(b: bytes) -> str:
    if len(b) != 6:
        raise ValueError("Ожидалось 6 байт для MAC")
    return ":".join(f"{x:02X}" for x in b)


class AFRConfiguratorRS232:
    """
    Конфигуратор по RS-232 согласно таблицам:
      - Запрос настроек (Query): PC-> 10 01 04 00 ; Module-> несколько ответов 13 01 LL TYPE 00 DATA...
      - Установка (Set): PC-> 20 01 TYPE LEN DATA...

    RS232 defaults:
      - baudrate: 9600 (для 3HZ 4CH: 115200)
      - bytesize: 8, parity: N, stopbits: 1
      - CRC: нет
    """

    # Константы протокола
    ID_PC_QUERY = 0x10
    ID_MODULE_QUERY_RESP = 0x13
    FC_QUERY = 0x01

    ID_PC_SET = 0x20
    FC_SET = 0x01

    TYPE_SRC_IP = 0x01
    TYPE_DST_IP = 0x02
    TYPE_SRC_PORT = 0x03
    TYPE_DST_PORT = 0x04
    TYPE_MAC = 0x05

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 0.5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None

    # --- Управление портом ---

    def open(self):
        if self.ser and self.ser.is_open:
            return
        self.ser = serial.Serial(
            self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # --- Низкоуровневые операции ---

    def _write(self, data: bytes) -> None:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("COM-порт не открыт")
        self.ser.reset_input_buffer()
        self.ser.write(data)
        self.ser.flush()

    def _read_some(self, n: int = 256) -> bytes:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("COM-порт не открыт")
        return self.ser.read(n)

    # --- Query ---

    def build_query_frame(self) -> bytes:
        """
        PC Send (из таблицы 3.1):
          ID=0x10, Function=0x01, Command Length=0x04, NC=0x00
        """
        return bytes([self.ID_PC_QUERY, self.FC_QUERY, 0x04, 0x00])

    @staticmethod
    def _parse_query_payload(payload: bytes) -> Optional[Dict[str, Any]]:
        """
        Ожидаем минимум: [TYPE][0x00][DATA...]
        TYPE: 1=SRC_IP(4), 2=DST_IP(4), 3=SRC_PORT(2), 4=DST_PORT(2), 5=MAC(6)
        """
        if len(payload) < 2:
            return None
        typ = payload[0]
        if payload[1] != 0x00:
            # NC по таблице = 0x00
            return None
        data = payload[2:]
        if typ == AFRConfiguratorRS232.TYPE_SRC_IP and len(data) >= 4:
            return {"src_ip": bytes_to_ip(data[:4])}
        if typ == AFRConfiguratorRS232.TYPE_DST_IP and len(data) >= 4:
            return {"dst_ip": bytes_to_ip(data[:4])}
        if typ == AFRConfiguratorRS232.TYPE_SRC_PORT and len(data) >= 2:
            port = (data[0] << 8) | data[1]
            return {"src_port": port}
        if typ == AFRConfiguratorRS232.TYPE_DST_PORT and len(data) >= 2:
            port = (data[0] << 8) | data[1]
            return {"dst_port": port}
        if typ == AFRConfiguratorRS232.TYPE_MAC and len(data) >= 6:
            return {"mac": bytes_to_mac(data[:6])}
        return None

    def query_settings(self, wait_total: float = 1.0) -> Dict[str, Any]:
        """
        Отправляет запрос и собирает ответы в течение окна ожидания.
        Возвращает dict: src_ip, dst_ip, src_port, dst_port, mac (если пришли).
        """
        fr = self.build_query_frame()
        self._write(fr)

        t0 = time.time()
        buf = bytearray()
        result: Dict[str, Any] = {}

        # Ждём несколько фреймов с ID=0x13,FC=0x01
        while time.time() - t0 < wait_total:
            piece = self._read_some(256)
            if not piece:
                continue
            buf += piece

            # Парсим возможные фреймы: [0x13][0x01][LEN][...LEN байт...]
            # LEN — длина payload. Из таблицы это «Command Length».
            i = 0
            while i + 3 <= len(buf):
                if buf[i] != self.ID_MODULE_QUERY_RESP or buf[i + 1] != self.FC_QUERY:
                    i += 1
                    continue
                ln = buf[i + 2]
                end = i + 3 + ln
                if end > len(buf):
                    # ждём догрузки
                    break
                payload = bytes(buf[i + 3:end])
                parsed = self._parse_query_payload(payload)
                if parsed:
                    result.update(parsed)
                i = end  # следующий фрейм

            # чистим обработанное начало буфера
            if i > 0:
                del buf[:i]

            # если мы уже собрали все поля — можно выходить
            have = {"src_ip", "dst_ip", "src_port", "dst_port", "mac"}
            if have.issubset(result.keys()):
                break

        return result

    # --- Setting ---

    def _build_set_frame(self, field_type: int, data: bytes) -> bytes:
        """
        Формат (таблица 3.2):
          [ID=0x20][FC=0x01][TYPE][LEN][DATA...]
        LEN — длина DATA в байтах.
        """
        if not (0 <= field_type <= 0xFF):
            raise ValueError("Некорректный тип поля")
        if not (0 <= len(data) <= 0xFF):
            raise ValueError("Длина данных >255 — не поддерживается протоколом")
        return bytes([self.ID_PC_SET, self.FC_SET, field_type & 0xFF, len(data) & 0xFF]) + data

    def _send_set_and_wait_ack(self, frame: bytes, ack_timeout: float = 0.5) -> bool:
        """
        Отправляет один SET-фрейм и ждёт краткий ответ.
        Эвристика успеха: в ответе встречается 0x00 после заголовка (Success/Failed флаг).
        У разных ревизий форматы ACK могут отличаться — при необходимости подгоните проверку.
        """
        self._write(frame)
        t0 = time.time()
        buf = bytearray()
        while time.time() - t0 < ack_timeout:
            piece = self._read_some(128)
            if piece:
                buf += piece
                # Простейшая эвристика успеха:
                # Ищем любую подпоследовательность вида [0x20][0x01][..][0x00]
                if b"\x20\x01" in buf and b"\x00" in buf:
                    return True
        # Если ACK не распознан, не считаем это фатальным — возвращаем False.
        return False

    def set_settings(self,
                     src_ip: Optional[str] = None,
                     src_port: Optional[int] = None,
                     dst_ip: Optional[str] = None,
                     dst_port: Optional[int] = 8001,
                     mac: Optional[str] = '00:08:AC:FF:FF:FF',
                     per_field_ack: bool = True) -> Dict[str, bool]:
        """
        Устанавливает переданные параметры. Возвращает dict с результатами по полям.
        Отправляются только те поля, которые не None.
        """
        results: Dict[str, bool] = {}

        if src_ip is not None:
            fr = self._build_set_frame(self.TYPE_SRC_IP, ip_to_bytes(src_ip))
            ok = self._send_set_and_wait_ack(fr) if per_field_ack else (self._write(fr) or True)
            results["src_ip"] = bool(ok)

        if dst_ip is not None:
            fr = self._build_set_frame(self.TYPE_DST_IP, ip_to_bytes(dst_ip))
            ok = self._send_set_and_wait_ack(fr) if per_field_ack else (self._write(fr) or True)
            results["dst_ip"] = bool(ok)

        if src_port is not None:
            if not (0 <= src_port <= 65535):
                raise ValueError("src_port вне диапазона 0..65535")
            fr = self._build_set_frame(self.TYPE_SRC_PORT, bytes([(src_port >> 8) & 0xFF, src_port & 0xFF]))
            ok = self._send_set_and_wait_ack(fr) if per_field_ack else (self._write(fr) or True)
            results["src_port"] = bool(ok)

        if dst_port is not None:
            if not (0 <= dst_port <= 65535):
                raise ValueError("dst_port вне диапазона 0..65535")
            fr = self._build_set_frame(self.TYPE_DST_PORT, bytes([(dst_port >> 8) & 0xFF, dst_port & 0xFF]))
            ok = self._send_set_and_wait_ack(fr) if per_field_ack else (self._write(fr) or True)
            results["dst_port"] = bool(ok)

        if mac is not None:
            fr = self._build_set_frame(self.TYPE_MAC, mac_to_bytes(mac))
            ok = self._send_set_and_wait_ack(fr) if per_field_ack else (self._write(fr) or True)
            results["mac"] = bool(ok)

        return results
    
if __name__ == "__main__":
    it=AFRConfiguratorRS232('COM1')
    settings=it.query_settings()
    print(settings)