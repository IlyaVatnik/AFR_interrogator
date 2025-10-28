# -*- coding: utf-8 -*-
"""

Created on Fri Oct 17 14:19:23 2025

@author: ChatGPT5 @ Ilya

For the AFR Arcadia Optronix Interrogator
"""
__version__='1.0'
__date__='27.10.2025'


import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from collections import deque
import numpy as np
import math


@dataclass
class InterrogatorUDPConfig:
    # IP/порт модуля и локальная привязка ПК
    module_ip: str = "192.168.0.19"
    module_port: int = 4567
    pc_bind_ip: str = "0.0.0.0"
    pc_bind_port: int = 8001

    # Размер сокетного буфера приёма (просим у ОС), ёмкость кольца и таймаут recv
    recv_buf_size: int = 4 * 1024 * 1024
    ring_size: int = 20000
    recv_timeout: float = 0.2  # seconds


class Interrogator:
    # Группа ID (наверх протокола делит пакеты на запросы/конфиг/рабочие)
    BASE_FREQ_GHZ = 191000   # базовая частота (зарезервировано; сейчас не используется)
    SCALE_GHZ_PER_COUNT = 1  # масштаб «тик->ГГц» (зарезервировано; сейчас не используется)

    ID_QUERY = 0x10   # запросы (чтение статических/конфигурационных параметров)
    ID_CONFIG = 0x20  # команды конфигурации (установка настроек)
    ID_WORK = 0x30    # рабочие (данные/старт/стоп/ADC)

    # Function codes по группам
    # 2.1 Query
    FC_READ_VERSION = 0x01
    FC_READ_SN = 0x03
    FC_READ_MODULE_PARAMS = 0x04
    FC_READ_SWEEP_CFG = 0x05
    FC_READ_CH_PARAMS = 0x06
    # 2.2 Config
    FC_SET_SWEEP = 0x01
    FC_SET_THRESHOLD = 0x02
    FC_SET_GAIN = 0x03
    FC_SET_PEAK_INTERVAL = 0x04
    FC_SAVE_THRESHOLD = 0x06
    # 2.3 Working
    FC_STOP = 0x01
    FC_READ_FREQ = 0x02
    FC_DEBUG = 0x03
    FC_READ_ADC_SINGLE = 0x07

    def __init__(self, cfg: InterrogatorUDPConfig = InterrogatorUDPConfig()):
        self.cfg = cfg
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Просим у ОС большой буфер приёма, чтобы не терять пакеты при пиковой нагрузке
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, cfg.recv_buf_size)
        except Exception:
            pass
        self._sock.bind((cfg.pc_bind_ip, cfg.pc_bind_port))
        self._sock.settimeout(cfg.recv_timeout)

        # Поток и флаг остановки для приёма рабочего потока 0x30 0x02
        self._rx_thread: Optional[threading.Thread] = None
        self._rx_stop = threading.Event()

        # Кольцевой буфер для частотных кадров и «последний» кадр
        self._ring = deque(maxlen=cfg.ring_size)
        self._ring_lock = threading.Lock()
        self._latest_freq_frame: Optional[dict] = None

        # Пользовательские колбэки, вызываются на каждый принятый кадр
        self._callbacks: List[Callable[[dict], None]] = []

        # Состояние/параметры модуля (часть подтягивается из запросов)
        self.channels: int = 4           # число каналов (читается из read_module_params)
        self.fbg_per_ch: int = 30        # число слотов FBG на канал (читается из read_module_params)
        self.peak_interval_ghz: int = 40 # интервал между пиками (ГГц), влияет на детекцию
        # Код скорости свипа (сырые 2 байта из мануала), по умолчанию 2 kHz
        self.sweep_speed_hex: Tuple[int, int] = (0x00, 0x66)
        self.module_params_known = False

        # Параметры свипа для справки/парсера (заполняются read_sweep_config/set_sweep)
        self.sweep_start_ghz: Optional[float] = None
        self.sweep_stop_ghz: Optional[float] = None
        self.sweep_step_ghz: Optional[float] = None    # шаг между FBG (из команды)
        self.ad_step_ghz: Optional[float] = None       # AD шаг сетки (между индексами)
        self.sweep_direction_decreasing: Optional[bool] = None  # True, если свип идёт в сторону убывания частоты
        
        # При инициализации отправим STOP, чтобы модуль прекратил поток, если он был включён ранее
        self.stop()

    # ------------------- Утилиты конвертации -------------------

    @staticmethod
    def freq_ghz_from_param(param: int) -> int:
        # Преобразование параметра свипа в частоту: Frequency(GHz) = 196251 - param
        return 196_251 - int(param)

    @staticmethod
    def param_from_freq_ghz(freq_ghz: int) -> int:
        # Обратное преобразование для команд конфигурации свипа
        return 196_251 - int(freq_ghz)

    @classmethod
    def freq_ghz_from_reported_u16(cls, v: int) -> float:
        # Зарезервировано для старых режимов: F_abs = BASE + SCALE * v
        return cls.BASE_FREQ_GHZ + cls.SCALE_GHZ_PER_COUNT * float(v)

    @staticmethod
    def ghz_to_nm(f_ghz: float) -> float:
        # λ[nm] = 299792458 / f[GHz]
        return 299_792_458.0 / f_ghz

    @staticmethod
    def to_le_u16(v: int) -> bytes:
        return struct.pack("<H", v & 0xFFFF)

    @staticmethod
    def to_be_u16(v: int) -> bytes:
        return struct.pack(">H", v & 0xFFFF)

    @staticmethod
    def from_u16_be(b: bytes, offset: int = 0) -> int:
        return struct.unpack_from(">H", b, offset)[0]

    @staticmethod
    def from_u16_le(b: bytes, offset: int = 0) -> int:
        return struct.unpack_from("<H", b, offset)[0]

    # ------------------- Отправка/прием -------------------

    def _send(self, payload: bytes):
        # «сырой» UDP sendto
        self._sock.sendto(payload, (self.cfg.module_ip, self.cfg.module_port))

    def _recv_datagram(self, timeout: Optional[float] = None) -> Optional[bytes]:
        # Блокирующий приём одного UDP датаграммы с таймаутом
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            data, _ = self._sock.recvfrom(65536)
            return data
        except socket.timeout:
            return None

    # ------------------- Высокоуровневые API -------------------

    def read_version(self, timeout: float = 1.0) -> float:
        # Команда: 0x10 0x01 0x04 0x00
        self._send(bytes([self.ID_QUERY, self.FC_READ_VERSION, 0x04, 0x00]))
        data = self._recv_datagram(timeout)
        if not data or len(data) < 8:
            raise TimeoutError("No response for version")
        # Ответ: 0x10 0x01 0x00 0x08 X1 X2 X3 X4 (версия * 100)
        if data[0] != self.ID_QUERY or data[1] != self.FC_READ_VERSION:
            raise ValueError("Unexpected packet for version")
        # Берём последние 4 байта как BE-целое, делим на 100.0
        x = data[-4:]
        raw = (x[0] << 24) | (x[1] << 16) | (x[2] << 8) | x[3]
        return raw / 100.0

    def read_sn(self, timeout: float = 1.0) -> int:
        # Команда: 0x10 0x03 0x04 0x00
        self._send(bytes([self.ID_QUERY, self.FC_READ_SN, 0x04, 0x00]))
        data = self._recv_datagram(timeout)
        if not data or len(data) < 8:
            raise TimeoutError("No response for SN")
        if data[0] != self.ID_QUERY or data[1] != self.FC_READ_SN:
            raise ValueError("Unexpected packet for SN")
        # Последние 4 байта — серийный номер (BE)
        x = data[-4:]
        sn = (x[0] << 24) | (x[1] << 16) | (x[2] << 8) | x[3]
        return sn

    def read_module_params(self, timeout: float = 1.0) -> dict:
        # Команда: 0x10 0x04 0x04 0x00
        self._send(bytes([self.ID_QUERY, self.FC_READ_MODULE_PARAMS, 0x04, 0x00]))
        data = self._recv_datagram(timeout)
        if not data or len(data) < 12:
            raise TimeoutError("No response for module params")
        if data[0] != self.ID_QUERY or data[1] != self.FC_READ_MODULE_PARAMS:
            raise ValueError("Unexpected packet for module params")

        # Формат ответа (хвост): SweepSpeed(2B) NoOfChannels(2B) NoOfFBG(2B) PeakInterval(2B) — все u16 BE
        ss, ch, fbg, pint = struct.unpack(">4H", data[-8:])
        # Обновляем состояние
        self.channels = ch
        self.fbg_per_ch = fbg
        self.peak_interval_ghz = pint
        self.sweep_speed_hex = (data[-8], data[-7])  # сырые байты кода скорости
        self.module_params_known = True

        # Таблица соответствия код скорости -> Гц (из мануала)
        sweep_speed_map = {
            (0x00, 0x00): 1,
            (0x00, 0x0A): 3,
            (0x00, 0x1E): 100,
            (0x00, 0x65): 200,
            (0x00, 0xC9): 500,
            (0x01, 0xF5): 1000,
            (0x00, 0x66): 2000,
            (0x00, 0xCA): 4000,
            (0x01, 0x92): 8000,
        }
        hz = sweep_speed_map.get((data[-8], data[-7]))
        return {
            "sweep_speed_code": (data[-8], data[-7]),
            "sweep_speed_hz": hz,
            "channels": ch,
            "fbg_per_channel": fbg,
            "peak_interval_ghz": pint,
        }

    def read_sweep_config(self, timeout: float = 1.0) -> dict:
        # Команда: 0x10 0x05 0x04 0x00
        self._send(bytes([self.ID_QUERY, self.FC_READ_SWEEP_CFG, 0x04, 0x00]))
        data = self._recv_datagram(timeout)
        if not data or len(data) < 12:
            raise TimeoutError("No response for sweep config")
        if data[0] != self.ID_QUERY or data[1] != self.FC_READ_SWEEP_CFG:
            raise ValueError("Unexpected packet for sweep config")
        # Хвост: StartParam, Step, StopParam, ADStep (u16 BE)
        start_p, step, stop_p, adstep = struct.unpack(">4H", data[-8:])
        start_freq = self.freq_ghz_from_param(start_p)
        stop_freq = self.freq_ghz_from_param(stop_p)

        # Сохраняем в состояние (для последующего анализа, если потребуется)
        self.sweep_start_ghz = float(start_freq)
        self.sweep_stop_ghz = float(stop_freq)
        self.sweep_step_ghz = float(step)
        self.ad_step_ghz = float(adstep)
        self.sweep_direction_decreasing = self.sweep_start_ghz > self.sweep_stop_ghz

        return {
            "start_param": start_p,
            "stop_param": stop_p,
            "step_ghz": step,
            "ad_step_ghz": adstep,
            "start_freq_ghz": start_freq,
            "stop_freq_ghz": stop_freq,
        }

    def read_channel_params(self, timeout: float = 1.0) -> List[dict]:
        # Команда: 0x10 0x06 0x04 0x00
        # Возвращает пороги и усиление по каналам в компактном виде (по 4 байта на канал).
        self._send(bytes([self.ID_QUERY, self.FC_READ_CH_PARAMS, 0x04, 0x00]))
        data = self._recv_datagram(timeout)
        if not data or len(data) < 8:
            raise TimeoutError("No response for channel params")
        if data[0] != self.ID_QUERY or data[1] != self.FC_READ_CH_PARAMS:
            raise ValueError("Unexpected packet for channel params")

        # Нормализация полезной нагрузки (на некоторых прошивках есть 2 служебных байта)
        payload = data[4:] if len(data) > 4 else b""
        if len(payload) % 4 != 0 and len(data) >= 6:
            payload = data[6:]

        # Разбор блоков [Threshold_u16_BE][Gain_u16_BE]
        # По наблюдениям:
        # - Gain: u16 BE. MSB (0x8000) — флаг ручного режима. LSB — уровень (0..5).
        #   Примеры:
        #   0x0005 → авто; уровень «5» (интерпретируется как авто-режим, уровень игнорируется)
        #   0x8002 → ручной; уровень 2 (manual_level=2)
        entries = []
        for i in range(0, len(payload), 4):
            if i + 4 > len(payload):
                break
            thr = (payload[i] << 8) | payload[i + 1]
            gain_hex = (payload[i + 2] << 8) | payload[i + 3]

            # Обрезка хвоста, если за каналами пришли нули (зависит от прошивки)
            if thr == 0 and gain_hex == 0 and len(entries) >= self.channels:
                break

            # Нынешняя логика разметки auto/manual — эвристическая и может быть улучшена.
            # Рекомендуемая интерпретация:
            #   gain_auto  = (gain_hex & 0x8000) == 0
            #   gain_level = (gain_hex & 0x00FF)
            # Текущий код ниже сохраняется для обратной совместимости, но желательно заменить.
            if hex(gain_hex)[2] == '8':
                gain_auto = False
                gain = int(hex(gain_hex)[5])
            else:
                gain_auto = True
                gain = int(gain_hex)

            entries.append({"channel": len(entries) + 1, "threshold": thr, "gain_auto": gain_auto, "gain": gain})
        return entries

    def set_sweep(self, start_freq_ghz: int, stop_freq_ghz: int, step_ghz: int = 2, ad_step_ghz: int = 2,
                  timeout: float = 1.0) -> bool:
        # Команда: 0x20 0x01 0x0C  StartParam(2B) Step(2B) StopParam(2B) ADStep(2B)
        # Требование модуля: start_freq > stop_freq.
        if start_freq_ghz <= stop_freq_ghz:
            raise ValueError("Start frequency must be > Stop frequency")
        start_p = self.param_from_freq_ghz(start_freq_ghz)
        stop_p = self.param_from_freq_ghz(stop_freq_ghz)
        payload = bytes([
            self.ID_CONFIG, self.FC_SET_SWEEP, 0x0C
        ]) + self.to_be_u16(start_p) + self.to_be_u16(step_ghz) + self.to_be_u16(stop_p) + self.to_be_u16(ad_step_ghz)
        self._send(payload)
        data = self._recv_datagram(timeout)
        ok = self._is_success_reply(data, self.ID_CONFIG, self.FC_SET_SWEEP)
        if ok:
            # Обновим локальное состояние — полезно для парсера и логики приложения
            self.sweep_start_ghz = float(start_freq_ghz)
            self.sweep_stop_ghz = float(stop_freq_ghz)
            self.sweep_step_ghz = float(step_ghz)
            self.ad_step_ghz = float(ad_step_ghz)
            self.sweep_direction_decreasing = self.sweep_start_ghz > self.sweep_stop_ghz
        return ok

    def set_threshold(self, channel: int, threshold: int, timeout: float = 1.0) -> bool:
        # Команда: 0x20 0x02 0x06  Ch(0-based)  ThrHi ThrLo
        # Порог 0..65535, 65535 обычно трактуется как «auto»
        if channel < 1 or channel > 16:
            raise ValueError("Channel must be 1..16")
        if not (0 <= threshold <= 65535):
            raise ValueError("Threshold must be 0..65535 (65535=auto)")
        ch_code = channel - 1
        thr = self.to_be_u16(threshold)
        payload = bytes([self.ID_CONFIG, self.FC_SET_THRESHOLD, 0x06, ch_code]) + thr
        self._send(payload)
        data = self._recv_datagram(timeout)
        return self._is_success_reply(data, self.ID_CONFIG, self.FC_SET_THRESHOLD)

    def set_gain(self, channel: int, auto: bool = True, manual_level: int = 0, timeout: float = 1.0) -> bool:
        # Команда: 0x20 0x03 0x06  Ch(0-based)  GainHi GainLo
        # Кодирование (по наблюдениям):
        #   auto=True  → Gain = 0x00LL (MSB=0), LL игнорируется прошивкой
        #   auto=False → Gain = 0x80LL (MSB=1), LL=manual_level (0..5)
        if channel < 1 or channel > 16:
            raise ValueError("Channel must be 1..16")
        if not (0 <= manual_level <= 5):
            raise ValueError("manual_level must be 0..5")
        ch_code = channel - 1
        if auto:
            gain = bytes([0x00, manual_level & 0xFF])
        else:
            gain = bytes([0x80, manual_level & 0xFF])
        payload = bytes([self.ID_CONFIG, self.FC_SET_GAIN, 0x06, ch_code]) + gain
        self._send(payload)
        data = self._recv_datagram(timeout)
        return self._is_success_reply(data, self.ID_CONFIG, self.FC_SET_GAIN)

    def set_peak_interval(self, ghz: int, timeout: float = 1.0) -> bool:
        # Команда: 0x20 0x04 0x04  Interval
        # Устанавливает «интервал пиков» (влияет на детектор). Допустимо 1..255 ГГц.
        if not (1 <= ghz <= 255):
            raise ValueError("Peak interval must be 1..255 GHz")
        payload = bytes([self.ID_CONFIG, self.FC_SET_PEAK_INTERVAL, 0x04, ghz & 0xFF])
        self._send(payload)
        data = self._recv_datagram(timeout)
        ok = self._is_success_reply(data, self.ID_CONFIG, self.FC_SET_PEAK_INTERVAL)
        if ok:
            self.peak_interval_ghz = ghz
        return ok

    def save_threshold(self):
        # Команда без ответа: 0x20 0x06 0x04 0x00 — сохранение порогов во FLASH/EEPROM
        self._send(bytes([self.ID_CONFIG, self.FC_SAVE_THRESHOLD, 0x04, 0x00]))

    def stop(self, timeout: float = 1.0) -> bool:
        # 0x30 0x01 0x06 0x00 0x00 0x00 — остановка рабочего потока
        self._send(bytes([self.ID_WORK, self.FC_STOP, 0x06, 0x00, 0x00, 0x00]))
        data = self._recv_datagram(timeout)
        return self._is_success_reply(data, self.ID_WORK, self.FC_STOP)

    def start_freq_stream(self, sweep_speed_code: Tuple[int, int] = (0x00, 0xCA)):
        # Команда запуска потока частот: 0x30 0x02 0x06  X1 X2 0x00
        # После неё модуль начинает циклически слать пакеты 0x30 0x02.
        self.sweep_speed_hex = sweep_speed_code
        payload = bytes([self.ID_WORK, self.FC_READ_FREQ, 0x06, sweep_speed_code[0], sweep_speed_code[1], 0x00])
        self._send(payload)
        # Запустить приёмный поток, если ещё не работает
        if not (self._rx_thread and self._rx_thread.is_alive()):
            self._rx_stop.clear()
            self._rx_thread = threading.Thread(target=self._rx_loop, name="FBG-UDP-RX", daemon=True)
            self._rx_thread.start()

    def register_callback(self, fn: Callable[[dict], None]):
        # Колбэк будет вызываться для каждого принятого и успешно распарсенного кадра 0x30 0x02
        self._callbacks.append(fn)

    def get_latest_freq_frame(self) -> Optional[dict]:
        # Возвращает последний принятый частотный кадр (без удаления из кольца)
        with self._ring_lock:
            return self._latest_freq_frame

    def pop_freq_frame(self) -> Optional[dict]:
        # Достаёт из кольца самый старый непрочитанный кадр (FIFO)
        with self._ring_lock:
            if self._ring:
                return self._ring.popleft()
            return None
        
    def get_data(self):
           # Упрощённый хелпер: возвращает (timestamp, список numpy-массивов длин волн по каналам),
           # где NaN удалены. Удобно для быстрых графиков/отладки.
           fr=self.pop_freq_frame()
           temp=[]
           for ch in range(self.channels):
               wavelengths=np.array([x for x in fr['wavelength_nm'][ch] if not math.isnan(x)])
               temp.append(wavelengths)
           return fr['timestamp'], temp 
           
           
        

    def debug_once(self, timeout: float = 1.0) -> Tuple[dict, dict]:
        # Вызов DEBUG (разовый). Текущая реализация-заглушка: реальный парсер требует уточнения формата.
        self._send(bytes([self.ID_WORK, self.FC_DEBUG, 0x06, 0x00, 0x00, 0x00]))
        freq_pkt = self._wait_for_packet(self.ID_WORK, self.FC_READ_FREQ, timeout)
        if not freq_pkt:
            raise TimeoutError("No frequency packet in debug")
        # TODO: распарсить содержимое debug-пакета согласно мануалу
        freq = 0
        adc = 0
        return freq, adc

    def read_single_channel_adc(self, channel: int, timeout: float = 1.0) -> dict:
        # Разовый запрос ADC по одному каналу (2.3.4).
        # TODO: распарсить содержимое пакета согласно мануалу
        if channel < 1 or channel > 16:
            raise ValueError("Channel must be 1..16")
        ch_code = (channel - 1) & 0xFF
        self._send(bytes([self.ID_WORK, self.FC_READ_ADC_SINGLE, 0x06, 0x00, 0x00, ch_code]))
        pkt = self._wait_for_packet(self.ID_WORK, self.FC_READ_ADC_SINGLE, timeout)
        if not pkt:
            raise TimeoutError("No ADC single packet")
        return self._parse_adc_single(pkt)

    # ------------------- Внутренние: приём/парсинг -------------------
    
    def _rx_loop(self):
        import time as _t
        pkt_counter = 0
        last_log = _t.perf_counter()
        recv_cnt = 0
    
        while not self._rx_stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65536)
                t_recv_perf = _t.perf_counter()
            except socket.timeout:
                continue
            except Exception as e:
                # Логируем и продолжаем, а не выходим
                print("RX exception:", repr(e))
                continue
            if not data:
                continue
            if data[0] == self.ID_WORK and data[1] == self.FC_READ_FREQ:
                # Пакетный счётчик, если прошивка его кладёт в [2:6] как BE32:
                pkt_ctr = (data[2] << 24) | (data[3] << 16) | (data[4] << 8) | data[5]
    
                frame = self._parse_freq_frame(data)
                if frame:
                    frame["t_perf"] = t_recv_perf
                    frame["pkt_counter_be32"] = pkt_ctr
                    with self._ring_lock:
                        self._latest_freq_frame = frame
                        self._ring.append(frame)
                    for cb in self._callbacks:
                        try:
                            cb(frame)
                        except Exception as e:
                            print("Callback error:", repr(e))
    
                    recv_cnt += 1
    
            # Периодический лог приёма — видеть «ямы»
            now = _t.perf_counter()
            if now - last_log >= 1.0:
                print(f"RX fps ~ {recv_cnt/(now-last_log):.0f} pkt/s, ring={len(self._ring)}/{self.cfg.ring_size}")
                recv_cnt = 0
                last_log = now


    def _wait_for_packet(self, id_byte: int, fc_byte: int, timeout: float) -> Optional[bytes]:
        # Ожидание одного пакета заданного ID/FC с таймаутом
        t0 = time.time()
        while time.time() - t0 < timeout:
            data = self._recv_datagram(timeout=max(0.0, timeout - (time.time() - t0)))
            if not data:
                continue
            if data[0] == id_byte and data[1] == fc_byte:
                return data
        return None

    @staticmethod
    def _is_success_reply(data: Optional[bytes], expect_id: int, expect_fc: int) -> bool:
        # Успех: совпали ID/FC, в конце два байта 0x00 0x01 (статус ACK)
        if not data or len(data) < 2:
            return False
        if data[0] != expect_id or data[1] != expect_fc:
            return False
        # Хвост подтверждения
        return data[-2:] == b"\x00\x01"
            


    def _parse_freq_frame(self, data: bytes):
        """
        Парсер кадра 0x30 0x02: Read FBG Frequency Data.

        Подтверждённый формат:
          [0]   = 0x30 (ID_WORK)
          [1]   = 0x02 (FC_READ_FREQ)
          [2:6] = 4 байта резерва (игнорируются; наблюдались как 00 00 01 EE)

          Далее по каналам подряд, без отдельного заголовка канала:
            для fbg in range(self.fbg_per_ch):
              [PeakID:1][Freq_u24_BE:3]
            [Temp_u16_BE:2]  # температура канала —  2 байта

        Интерпретация частоты:
          - Freq_u24_BE — 24-битное беззнаковое big-endian значение.
          - Единица измерения — 0.1 ГГц (десятые ГГц).
          - Преобразования:
              f_ghz = u24 / 10.0
              lambda_nm = 299_792_458.0 / f_ghz

        Пустые слоты FBG:
          - Передаются как PeakID=N, Freq_u24=0x000000 — интерпретируем как «нет пика», ставим NaN.

        """
        try:
            # Проверка заголовка и минимальной длины (ID/FC + 4B резерва)
            ID_WORK = getattr(self, "ID_WORK", 0x30)
            FC_READ_FREQ = getattr(self, "FC_READ_FREQ", 0x02)
            if len(data) < 6 or data[0] != ID_WORK or data[1] != FC_READ_FREQ:
                return None
    
            channels = self.channels
            fbg_per_ch = self.fbg_per_ch
            if channels <= 0 or fbg_per_ch <= 0:
                return None
    
            # Константы и помощники
            C_M_PER_S = 299_792_458.0  # м/с; λ[nm] = C / f[GHz]
            def be_u24(b: bytes, off: int) -> int:
                # Big-endian 24-bit: b[off] — старший байт
                return (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]
            def to_i8(x: int) -> int:
                # Вспомогательно: перевод беззнакового 8-битного в знаковый (если понадобится)
                return x - 256 if x > 127 else x
    
            # Начальное смещение после заголовка и 4B резерва
            o = 6  # пропускаем [0]=ID, [1]=FC и [2:6]=резерв
    
            # Готовим выходные структуры
            peak_ids_all = []       # [ch][fbg] -> PeakID (0..255)
            freqs_u24_all = []      # [ch][fbg] -> u24 (0..16_777_215)
            freqs_ghz_all = []      # [ch][fbg] -> float GHz (или NaN)
            lambdas_nm_all = []     # [ch][fbg] -> float nm (или NaN)
            temps_raw = []          # [ch] -> u16 BE (масштаб в °C уточняется; обычно /100)
            temps_i8 = []           # [ch] -> зарезервировано, для совместимости интерфейса
    
            # Оценим минимально необходимую длину (если пакет полный)
            per_fbg_size = 4  # PeakID(1) + Freq_u24(3)
            per_channel_min = fbg_per_ch * per_fbg_size + 2  # + Temp_u16_BE(2)
            min_len = 6 + channels * per_channel_min
            # Если пакет короче — не рвём парсинг, добиваем NaN/нулями.
    
            for ch in range(channels):
                ch_peak_ids, ch_u24, ch_f, ch_lam = [], [], [], []
    
                # FBG-блоки канала
                for _ in range(fbg_per_ch):
                    if o + 4 > len(data):
                        # Не хватает данных — добиваем «пустышками»
                        ch_peak_ids.append(0)
                        ch_u24.append(0)
                        ch_f.append(float("nan"))
                        ch_lam.append(float("nan"))
                        continue
    
                    peak_id = data[o]
                    u24 = be_u24(data, o + 1)
                    o += 4
    
                    ch_peak_ids.append(peak_id)
                    ch_u24.append(u24)
    
                    if u24 == 0:
                        ch_f.append(np.nan)
                        ch_lam.append(np.nan)
                    else:
                        f_ghz = u24 / 10.0
                        lam_nm = C_M_PER_S / f_ghz
                        ch_f.append(f_ghz)
                        ch_lam.append(lam_nm)
    
                # Температура канала: 2 байта u16 BE (ВАЖНО: раньше читали 1 байт — это было неверно)
                if o + 2 <= len(data):
                    t_raw = (data[o] << 8) | data[o + 1]
                    o += 2
                else:
                    t_raw = 0  # недостающие данные — ставим 0, чтобы сохранить тип
    
                peak_ids_all.append(ch_peak_ids)
                freqs_u24_all.append(ch_u24)
                freqs_ghz_all.append(ch_f)
                lambdas_nm_all.append(ch_lam)
                temps_raw.append(t_raw)
                # temps_i8 здесь не имеет физического смысла; оставлен для совместимости структуры
                temps_i8.append(to_i8(t_raw & 0xFF))
    
            # valid_ratio — доля валидных частот (конечные значения)
            total_points = channels * fbg_per_ch
            valid_points = sum(
                1 for row in freqs_ghz_all for x in row
                if isinstance(x, (int, float)) and math.isfinite(x)
            )
            valid_ratio = (valid_points / total_points) if total_points else 0.0
    
            return {
                "id": data[0],
                "fc": data[1],
                "channels": channels,
                "fbg_per_channel": fbg_per_ch,
    
                "peak_id": peak_ids_all,          # [ch][fbg] PeakID
                "freqs_u24_raw": freqs_u24_all,   # [ch][fbg] u24 из кадра (0.1 ГГц)
                "freqs_ghz": freqs_ghz_all,       # [ch][fbg] f = u24/10.0
                "wavelength_nm": lambdas_nm_all,  # [ch][fbg] λ = C/f
    
                "temps_raw_u8": temps_raw,        # [ch] u16 BE (масштаб -> °C по месту использования)
                "temps_i8": temps_i8,             # [ch] зарезервировано (историческое поле)
    
                "sweep": None,            # в этом кадре sweep-параметров нет
                "offset": None,
                "valid_ratio": valid_ratio,
                "timestamp": time.time(),
            }

        except Exception as e:
            print("parse_freq_frame(0x30,0x02) error:", repr(e), "len=", len(data))
            return None
       
        
    def _parse_debug_frame(self, data: bytes) -> dict:
        # Формат 2.3.3 (по мануалу): 0x30 0x03 ... затем по каналу:
        # ChIndex(1B), Gain(1B), далее 2551*2B ADC значений (u16 BE).
        # TODO: распарсить содержимое debug-пакета согласно мануалу
        ch = self.channels
        payload = data[2:]
        # На некоторых прошивках присутствуют 2 байта длины — сдвиг на 2
        if len(payload) < 2 or not (0 <= payload[0] <= 0x10):
            if len(data) > 4:
                payload = data[4:]

        adc_per_ch = 2551  # при AD step=2 GHz, согласно мануалу
        bytes_per_ch = 2 + adc_per_ch * 2  # ch+gain + ADC u16
        result = {"channels": []}
        off = 0
        for _ in range(ch):
            if off + bytes_per_ch > len(payload):
                break
            ch_idx = payload[off]
            gain = payload[off + 1]
            off += 2
            adc = []
            for _i in range(adc_per_ch):
                if off + 2 > len(payload):
                    break
                v = (payload[off] << 8) | payload[off + 1]
                adc.append(v)
                off += 2
            result["channels"].append({"channel": ch_idx + 1, "gain_hex": gain, "adc_u16": adc})
        return result

    def _parse_adc_single(self, data: bytes) -> dict:
        # 2.3.4: ответ содержит: ... ChannelNo/Gain (вариативно по прошивке) + ADC1..ADC2551 (u16)
        # TODO: распарсить содержимое debug-пакета согласно мануалу
        adc_count = 2551
        adc_bytes = adc_count * 2
        # Смещение полезной нагрузки зависит от реализации прошивки — пробуем две схемы
        if len(data) < adc_bytes + 6:
            payload = data[4:]
        else:
            payload = data[2:]
        if len(payload) < adc_bytes + 4:
            raise ValueError("ADC single payload too short")
        ch_no = payload[2] if len(payload) > 3 else 0
        gain = payload[3] if len(payload) > 3 else 0
        adc_raw = payload[-adc_bytes:]
        adc = [(adc_raw[i] << 8) | adc_raw[i + 1] for i in range(0, len(adc_raw), 2)]
        return {"channel": (ch_no & 0xFF) + 1, "gain_hex": gain & 0xFF, "adc_u16": adc}

    # ------------------- Утилита: карты скоростей -------------------

    @staticmethod
    def sweep_speed_code(hz: int) -> Tuple[int, int]:
        # Таблица соответствия требуемой скорости свипа → код (из мануала, табл. 2.1.3/2.3.2)
        mapping = {
            1: (0x00, 0x00),
            3: (0x00, 0x0A),
            100: (0x00, 0x1E),
            200: (0x00, 0x65),
            500: (0x00, 0xC9),
            1000: (0x01, 0xF5),
            2000: (0x00, 0x66),
            4000: (0x00, 0xCA),
            8000: (0x01, 0x92),
        }
        if hz not in mapping:
            raise ValueError("Unsupported sweep speed")
        return mapping[hz]
    
    '''
    '''
 


#%%
# ------------------- Пример использования -------------------

if __name__ == "__main__":
    it = Interrogator()
#%%
    # Прочитать идентификацию и параметры
    ver = it.read_version()
    sn = it.read_sn()
    mod = it.read_module_params()
    sweep = it.read_sweep_config()  # это также заполнит локальные поля свипа
    ch_params=it.read_channel_params()
    print(f"Version: {ver}, SN: {sn}")
    print("Module params:", mod)
    print("Sweep cfg:", sweep)
    print("Channel params:", ch_params)

    # Настроить sweep (пример: 196250 -> 191150, шаги по умолчанию 2 ГГц)
    ok = it.set_sweep(start_freq_ghz=196250, stop_freq_ghz=191150, step_ghz=2, ad_step_ghz=2)
    print("Set sweep:", ok)

    # Порог/усиление: показываем настройку канала 2 и «глушим» прочие завышенным порогом
    # Внимание: для ручного усиления MSB=1 (0x80xx), для авто — MSB=0 (0x00xx).
    # Уровень (LL) в авто-режиме прошивкой игнорируется.
    ch=2
    it.set_threshold(ch, 2000)
    it.set_gain(ch, auto=False, manual_level=0)
    # Рекомендуется отключить ненужные каналы завышенным порогом:
    for ch in (1,3,4): it.set_threshold(ch, 60000)

    # Запуск потока частот, чтение одного кадра и быстрый доступ к данным
    it.start_freq_stream()
    fr=it.pop_freq_frame()
    time_stamp,data=it.get_data()
    data=data[ch-1]
    print(time_stamp,data)
    it.stop()
    
   
    
