# -*- coding: utf-8 -*-
"""
Created on Tue Oct 28 11:31:22 2025

@author: Илья
"""

import serial
import time
from typing import Optional, Dict, Any

__version__='1.1'
__date__='30.10.2025'


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
    ID_MODULE_QUERY_RESP = 0x10
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
        self.open()

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
            # parity=serial.PARITY_EVEN,
            # parity=serial.PARITY_ODD,
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
            buf = self._read_some(256)
            if not buf:
                continue
            
            
            if buf[0] != self.ID_MODULE_QUERY_RESP or buf[1] != self.FC_QUERY:
                continue
            total_len = int.from_bytes(buf[ 2:4], "big")
            result['src_ip']=(".".join(str(x) for x in buf[4:8]))
            result['src_port']= (buf[8] << 8) | buf[9]
            result['dst_ip']= (".".join(str(x) for x in buf[10:14]))
            result['dst_port']= (buf[14] << 8) | buf[15]
            result['MAC']= ":".join(f"{x:02X}" for x in buf[16:22])
            
            
        return result

    # --- Setting ---

 

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
                if buf[5]==0x01:
                    print('Параметры успешно установлены')
                    return True
                elif buf[5]==0x00:
                    print('Параметры не установлены')
                    return False
                
        # Если ACK не распознан, не считаем это фатальным — возвращаем False.
        return False

    def set_settings(self,
                     src_ip: Optional[str] = None,
                     dst_ip: Optional[str] = '192.168.0.1',
                     src_port: Optional[int] = 4567,
                     dst_port: Optional[int] = 8001,
                     mac: Optional[str] = '00:08:04:FE:DB:B6',
                     per_field_ack: bool = True) -> Dict[str, bool]:
        """
        Устанавливает переданные параметры. Возвращает dict с результатами по полям.
        Отправляются только те поля, которые не None.
        """
        
        
        fr=bytes([0x20,0x01,0x16])+ip_to_bytes(src_ip)+src_port.to_bytes(2)+ip_to_bytes(dst_ip)+dst_port.to_bytes(2) + mac_to_bytes(mac)
        
        ok = self._send_set_and_wait_ack(fr)
        
        return ok
   #%%
if __name__ == "__main__":
    it=AFRConfiguratorRS232('COM5')
    #%%
    settings=it.query_settings()
    print(settings)

#%%

    it.set_settings(src_ip='10.2.60.38',dst_ip='10.2.60.33')
    
    #%%
    settings=it.query_settings()
    print(settings)
