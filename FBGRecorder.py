# -*- coding: utf-8 -*-
"""
FBGrecorder.py — безопасная безголовая запись потока FBG в файл и live-плот.

Формат файла:
  - header (pickle: dict)
    {
      "channels": int,
      "fbg_per_ch": int,
      "version": 4,
      "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_ch][fbg])"
    }
  - затем многократно блоки: [uint32_be length] + [pickle.dumps(batch)]
    где batch = List[Tuple[float ts_perf, float ts_unix, int pkt_ctr, List[List[float]]]]

Чтение: read_fbg_stream_raw_lp(filepath) — устойчиво к оборванному хвосту.
Live-плот: live_plot_wavelengths(it, channel, fbg_indices, ...) — запускайте из главного потока GUI.
"""

__version__='1.7'
__date__='2026.02.27'



# from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from queue import Queue, Empty, Full
from typing import Any, Callable, Dict, List, Optional, Tuple,Iterable
from matplotlib.animation import FuncAnimation
import gc
import numpy as np
from pathlib import Path

# Мягкие зависимости
try:
    import pickle
except Exception as e:
    raise RuntimeError("pickle недоступен") from e


# ==========================
# Конфигурации и статистика
# ==========================


@dataclass
class RecorderConfig:
    filepath: str
    duration_sec: float
    batch_size: int = 1000
    queue_max: int = 50000
    fsync_every_batches: int = 20
    idle_sleep_empty_ring: float = 0.0002
    log_stats_interval: float = 0.5
    start_delay_sec: float = 0.5
    warmup_sec: float = 1.5
    drop_during_warmup: bool = True
    min_rate_hz: float = 0.0
    rate_window_sec: float = 0.8
    disable_gc_during_record: bool = True

    # Новый параметр: какой канал писать (None — все)
    channels: Optional[List[int]] = None,        # 1-based
    FBGs: Optional[List[List[int]]] = None, # 1-based
    
    record_channel: Optional[int] = None
    # Расширенная фильтрация: список каналов и FBG (0-based). Если None — писать всё.
    record_channels: Optional[List[int]] = None
    record_fbg_map: Optional[List[List[int]]] = None
    
    # Новый параметр: записывать только каждый n-ый кадр (после прогрева). 1 = писать каждый.
    write_every_n: int = 1
    other_params: Optional[Dict] = None # любые другие параметры, которые надо сохранить в файл


@dataclass
class RecorderStats:
    started_at: float = field(default_factory=lambda: time.perf_counter())
    rx_frames: int = 0
    wr_frames: int = 0
    rx_drops: int = 0
    rx_fps: float = 0.0
    wr_fps: float = 0.0
    ring_len: int = 0
    blocks_written: int = 0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "rx_frames": self.rx_frames,
            "wr_frames": self.wr_frames,
            "rx_drops": self.rx_drops,
            "rx_fps": self.rx_fps,
            "wr_fps": self.wr_fps,
            "ring_len": self.ring_len,
            "blocks_written": self.blocks_written,
        }


# ==========================
# Утилиты
# ==========================
def make_header(it: Any,
                channel_map: Optional[List[int]] = None,
                fbg_map: Optional[List[List[int]]] = None,
                other_params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Строит заголовок.

    version=4: без карт выбора
    version=5: хранит 0-based channel_map/fbg_map (как раньше уже сделано)
    version=6: дополнительно хранит 1-based channel_list/FBGs_list (то, что просили)

    Поля:
      - channel_map: List[int] (0-based индексы каналов)
      - fbg_map: List[List[int]] (0-based индексы решёток)
      - channel_list: List[int] (1-based каналы, записанные в файл)
      - FBGs_list: List[List[int]] (1-based решётки, записанные в файл)
    """
    orig_channels = int(getattr(it, "channels", 0))
    orig_fbg = int(getattr(it, "fbg_per_ch", 0))

    hdr: Dict[str, Any] = {
        "channels": orig_channels,
        "fbg_per_ch": orig_fbg,
        "version": 4,
        "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_ch][fbg])",
    }

    if channel_map is not None or fbg_map is not None:
        # --- 0-based карты (внутреннее представление) ---
        cm = list(map(int, channel_map or list(range(orig_channels))))

        if fbg_map is None:
            fm = [list(range(orig_fbg)) for _ in cm]
        else:
            fm = [list(map(int, arr)) for arr in fbg_map]

        # --- 1-based списки (то, что нужно пользователю) ---
        channel_list = [c + 1 for c in cm]
        FBGs_list = [[i + 1 for i in row] for row in fm]

        hdr.update({
            "version": 6,  # было 5, стало 6 (расширили заголовок)
            "original_channels": orig_channels,
            "original_fbg_per_ch": orig_fbg,

            "channel_map": cm,     # 0-based
            "fbg_map": fm,         # 0-based

            "channel_list": channel_list,  # 1-based (НОВОЕ)
            "FBGs_list": FBGs_list,        # 1-based (НОВОЕ)

            "channels": len(cm),   # число выбранных каналов в данных
            # оставляем fbg_per_ch как «исходное» для совместимости старого кода/ожиданий
            "fbg_per_ch": orig_fbg,
            "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_selected_ch][n_selected_fbg_per_ch])",
            'other_params': other_params
        })

    return hdr


def _write_block(fh, batch: List[Tuple[float, float, int, List[List[float]]]]) -> int:
    """Записать один length-prefixed блок с batch. Возвращает 1, если что-то записано, иначе 0."""
    if not batch:
        return 0
    blob = pickle.dumps(batch, protocol=pickle.HIGHEST_PROTOCOL)
    fh.write(struct.pack(">I", len(blob)))
    fh.write(blob)
    return 1

def configure_headless_matplotlib() -> None:
    """
    Переводит Matplotlib в headless-режим, закрывает все окна.
    Вызывайте перед записью, если в процессе ранее был GUI.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        try:
            plt.close("all")
        except Exception:
            pass
    except Exception:
        pass


# ==========================
# Основной класс рекордера
# ==========================

class FBGRecorder:
    """
    Безголовый двухпоточный рекордер.
    - rx_thread дренирует it.pop_freq_frame() и кладёт записи в очередь
    - wr_thread пишет length-prefixed батчами в файл

    Методы:
      start() — запустить потоки записи
      stop() — запросить остановку и дождаться завершения writer
      stats() — текущая статистика
      wait_done(timeout=None) — ждать окончания записи
      is_running — флаг активности writer
    """

    def __init__(self, it: Any, cfg: RecorderConfig):
        self.it = it
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._q: Queue = Queue(maxsize=cfg.queue_max)
        self._rx_thread: Optional[threading.Thread] = None
        self._wr_thread: Optional[threading.Thread] = None
        self._stats = RecorderStats()
        self._gc_was_enabled: bool = False

    def start(self) -> None:
        if self._rx_thread or self._wr_thread:
            raise RuntimeError("Recorder уже запущен")

        # Время старта
        self._stats.started_at = time.perf_counter()

        # Небольшая пауза после запуска потока устройства
        if self.cfg.start_delay_sec > 0:
            time.sleep(self.cfg.start_delay_sec)

        # Отключим GC (опционально) на время записи
        import gc
        self._gc_was_enabled = gc.isenabled()
        if self.cfg.disable_gc_during_record and self._gc_was_enabled:
            gc.disable()

        self._wr_thread = threading.Thread(target=self._writer_loop, name="FBGWriter", daemon=True)
        self._rx_thread = threading.Thread(target=self._rx_loop, name="FBGRxDrain", daemon=True)
        self._wr_thread.start()
        self._rx_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._done_event.wait(timeout=max(1.0, self.cfg.duration_sec + 2.0))

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        return self._done_event.wait(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return not self._done_event.is_set()

    def stats(self) -> Dict[str, Any]:
        return self._stats.snapshot()

    # ------------- внутренние циклы -------------


                
    def _rx_loop(self) -> None:
        """
        Быстрый дренаж кольца в очередь.
        В очередь кладутся кортежи (ts_perf, ts_unix, pkt_ctr, wl).
        """
        last_rx = time.perf_counter()
        rx_count_since = 0
    
        while not self._stop_event.is_set():
            try:
                fr = self.it.pop_freq_frame()
    
                # Ничего не пришло — подождём чуть-чуть
                if not fr:
                    time.sleep(self.cfg.idle_sleep_empty_ring)
                    continue
    
                # Ожидаем dict-подобный объект
                if not isinstance(fr, dict):
                    # Иногда драйверы отдают NamedTuple/объект с атрибутами — попробуем привести
                    try:
                        fr = dict(fr)  # может сработать для пар/Mapping
                    except Exception:
                        # как минимум не уронить поток
                        continue
    
                wl = fr.get("wavelength_nm")
                if wl is None or not isinstance(wl, (list, tuple)) or len(wl) == 0:
                    continue

                # Приведём строки к list[float]
                def _row_to_list(row):
                    if not isinstance(row, (list, tuple)):
                        try:
                            row = list(row)
                        except Exception:
                            row = []
                    return [float(x) for x in row]

                # Применим фильтрацию
                wl_filtered: List[List[float]]
                if self.cfg.record_channels is not None or self.cfg.record_channel is not None:
                    # приоритет у record_channels; record_channel — обратная совместимость
                    if self.cfg.record_channels is not None:
                        ch_map = [int(x) for x in self.cfg.record_channels]
                    else:
                        ch_map = [int(self.cfg.record_channel)]  # type: ignore[arg-type]

                    # Проверим границы
                    wl_filtered = []
                    for idx, ch in enumerate(ch_map):
                        if 0 <= ch < len(wl):
                            row_full = _row_to_list(wl[ch])
                        else:
                            row_full = []

                        if self.cfg.record_fbg_map is not None:
                            fbg_map_for_ch = self.cfg.record_fbg_map[idx] if idx < len(self.cfg.record_fbg_map) else []
                            row_sel = []
                            for fbg in fbg_map_for_ch:
                                if 0 <= fbg < len(row_full):
                                    row_sel.append(row_full[fbg])
                                else:
                                    row_sel.append(float("nan"))
                            wl_filtered.append(row_sel)
                        else:
                            wl_filtered.append(row_full)
                else:
                    # без фильтрации — все каналы/все FBG
                    wl_filtered = [_row_to_list(r) for r in wl]

                ts_perf = float(fr.get("t_perf", time.perf_counter()))
                ts_unix = float(fr.get("timestamp", time.time()))
                pkt_ctr = int(fr.get("pkt_counter_be32", -1))
                rec = (ts_perf, ts_unix, pkt_ctr, wl_filtered)               
    
                try:
                    self._q.put_nowait(rec)
                except Full:
                    # Переполнение — удалить старейший и попробовать снова
                    try:
                        _ = self._q.get_nowait()
                    except Empty:
                        pass
                    try:
                        self._q.put_nowait(rec)
                    except Full:
                        self._stats.rx_drops += 1
    
                self._stats.rx_frames += 1
                rx_count_since += 1
    
                now = time.perf_counter()
                if (now - last_rx) >= self.cfg.log_stats_interval:
                    self._stats.rx_fps = rx_count_since / (now - last_rx)
                    rx_count_since = 0
                    last_rx = now
    
                # оценка длины ring (без лока, не критично)
                try:
                    self._stats.ring_len = len(self.it._ring)  # type: ignore[attr-defined]
                except Exception:
                    pass
    
            except Exception:
                # Любая неожиданная ситуация не должна убить RX-поток
                # Немного подождём и продолжим
                time.sleep(self.cfg.idle_sleep_empty_ring)
                continue

    def _writer_loop(self) -> None:
        """
        Запись length-prefixed батчей в файл с фазой прогрева/стабилизации.
        """
        import gc

        t_start = self._stats.started_at
        t_end = t_start + float(self.cfg.duration_sec)
        blocks_written = 0
        wr_count_since = 0
        last_wr = time.perf_counter()

        writing_active = False
        warmup_deadline = t_start + max(0.0, self.cfg.warmup_sec)
        write_every_n = max(1, int(getattr(self.cfg, "write_every_n", 1)))
        taken_ctr = 0  # считанные (и прошедшие warmup) кадры, для отбора каждого n-ого

        def warmup_done(now: float) -> bool:
            if now >= warmup_deadline:
                return True
            if self.cfg.min_rate_hz > 0.0:
                return self._stats.rx_fps >= self.cfg.min_rate_hz
            return False

        try:
            with open(self.cfg.filepath, "wb") as f:
                # channel_map/fbg_map для заголовка (0-based)
                ch_map = None
                fbg_map = None
                if self.cfg.record_channels is not None:
                    ch_map = [int(x) for x in self.cfg.record_channels]
                    fbg_map = None
                    if self.cfg.record_fbg_map is not None:
                        # убедимся, что длины соотносятся
                        fbg_map = [list(map(int, arr)) for arr in self.cfg.record_fbg_map]
                elif self.cfg.record_channel is not None:
                    ch_map = [int(self.cfg.record_channel)]

                header = make_header(self.it, channel_map=ch_map, fbg_map=fbg_map,other_params=self.cfg.other_params)
                pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)
         

                batch: List[Tuple[float, float, int, List[List[float]]]] = []

                def flush_batch():
                    nonlocal blocks_written, batch
                    wrote = _write_block(f, batch)
                    if wrote:
                        blocks_written += 1
                        self._stats.blocks_written = blocks_written
                        batch.clear()
                        if self.cfg.fsync_every_batches and (blocks_written % self.cfg.fsync_every_batches == 0):
                            f.flush()
                            os.fsync(f.fileno())

                while not self._stop_event.is_set():
                    now = time.perf_counter()
                    if now >= t_end:
                        break

                    if not writing_active and warmup_done(now):
                        writing_active = True

                    try:
                        timeout = min(0.1, max(0.0, t_end - now))
                        rec = self._q.get(timeout=timeout)
                    except Empty:
                        if writing_active:
                            flush_batch()
                        continue

                    if not writing_active and self.cfg.drop_during_warmup:
                        # В прогреве просто выкидываем кадры, чтобы не копить задержку
                        continue


                    # Если уже не прогрев — считаем только эти кадры
                    if writing_active:
                        taken_ctr += 1
                        if (taken_ctr % write_every_n) != 0:
                            # пропускаем этот кадр
                            continue                    

                    batch.append(rec)
                    self._stats.wr_frames += 1
                    wr_count_since += 1

                    if writing_active and (len(batch) >= self.cfg.batch_size):
                        flush_batch()

                    if (now - last_wr) >= self.cfg.log_stats_interval:
                        self._stats.wr_fps = wr_count_since / (now - last_wr)
                        wr_count_since = 0
                        last_wr = now

                # финальный сброс
                if writing_active:
                    flush_batch()
                f.flush()
                os.fsync(f.fileno())
        finally:
            try:
                if self.cfg.disable_gc_during_record and not gc.isenabled() and self._gc_was_enabled:
                    gc.enable()
            except Exception:
                pass
            self._done_event.set()


# ==========================
# Обёртка высокого уровня
# ==========================
def record_to_file(it: Any,
                   filepath: str,
                   duration_sec: float,
                   channels: Optional[List[int]] = None,        # 1-based
                   FBGs: Optional[List[List[int]]] = None ,# 1-based
                   write_every_n: int = 1,
                   other_params: Optional[Dict] = None
                   ) -> Dict[str, Any]:
    """
    Если заданы channels/FBGs — записывается только это подмножество.
    channels — список каналов (1-based). FBGs — список списков FBG (1-based) на каждый канал.
    Если channels задан, а FBGs — нет: будут записаны все FBG выбранных каналов.
    """
    
    batch_size=1000
    queue_max= 50000
    fsync_every_batches= 20
    idle_sleep_empty_ring= 0.0002
    start_delay_sec= 0.5
    warmup_sec= 1.5
    drop_during_warmup= True
    min_rate_hz= 1500.0
    rate_window_sec= 0.8
    disable_gc_during_record=True
    
    
    
    # configure_headless_matplotlib()
    import gc as _gc
    _gc.collect()

    # Проверка и преобразование индексов в 0-based
    rec_channels_zb: Optional[List[int]] = None
    rec_fbg_map_zb: Optional[List[List[int]]] = None

    if channels is not None:
        if not isinstance(channels, (list, tuple)) or len(channels) == 0:
            raise ValueError("channels должен быть непустым списком (1-based)")
        rec_channels_zb = [int(ch) - 1 for ch in channels]
        if FBGs is not None:
            if len(FBGs) != len(rec_channels_zb):
                raise ValueError("Длина FBGs должна совпадать с длиной channels")
            rec_fbg_map_zb = []
            for lst in FBGs:
                if not isinstance(lst, (list, tuple)) or len(lst) == 0:
                    raise ValueError("Каждый элемент FBGs должен быть непустым списком (1-based)")
                rec_fbg_map_zb.append([int(i) - 1 for i in lst])
        else:
            rec_fbg_map_zb = None

    cfg = RecorderConfig(
        filepath=filepath,
        duration_sec=duration_sec,
        batch_size=batch_size,
        queue_max=queue_max,
        fsync_every_batches=fsync_every_batches,
        idle_sleep_empty_ring=idle_sleep_empty_ring,
        start_delay_sec=start_delay_sec,
        warmup_sec=warmup_sec,
        drop_during_warmup=drop_during_warmup,
        min_rate_hz=min_rate_hz,
        rate_window_sec=rate_window_sec,
        disable_gc_during_record=disable_gc_during_record,
        # обратная совместимость: если задан один канал и не задан список — используем старое поле
        channels=channels,
        FBGs=FBGs,
        record_channels=rec_channels_zb,
        record_fbg_map=rec_fbg_map_zb,
        write_every_n=int(write_every_n),
        other_params=other_params
    )

    rec = FBGRecorder(it, cfg)
    rec.start()
    rec.wait_done(timeout=duration_sec + 10.0)
    rec.stop()
    return rec.stats()


def record_spectra_to_file(it: Any,
                           filepath: str,
                           duration_sec: float,
                           write_every_n: int = 1,
                           other_params: Optional[Dict] = None,
                           channels: Optional[List[int]] = None        # 1-based
                           ) -> None:
    
    dtype=np.float32
    
    n_ch=len(channels)
    
    max_acq_rate=2000 # Hz
    max_cols=int(duration_sec*max_acq_rate/1/n_ch)
    print(max_cols)
    data_path = Path(filepath)
    meta_path = data_path.with_suffix(data_path.suffix+'.meta')
    
    waves=it.get_waves()
    n_rows=len(waves)
    
    mm = np.memmap(data_path, dtype=dtype, mode="w+", shape=(n_ch, n_rows, max_cols))
    time_start=time.time()
    time_current=time_start
    times_array=[]
    jj=0
    while time_current-time_start<duration_sec:
        for ii,ch in enumerate(channels):
            # print(ii,jj,max_cols)
            spectrum=it.get_single_spectrum(ch)
            mm[ii,:,jj]=spectrum
        times_array.append(time_current-time_start)
        time_current=time.time()
        jj+=1
  
    times_array=np.array(times_array)
    mm.flush()
    dictionary={}
    dictionary['n_ch']=n_ch
    dictionary['n_rows']=n_rows
    dictionary['n_cols']=jj
    dictionary['max_cols']=max_cols
    dictionary['times']=times_array
    dictionary['waves']=waves
    dictionary['dtype']=dtype
    dictionary['other_params']=other_params
    with open(meta_path,'wb') as f:
        pickle.dump(dictionary,f)

    
    
def read_spectra_from_file(filepath:str,
                           channel:int):
    data_path = Path(filepath)
    meta_path = data_path.with_suffix(data_path.suffix+'.meta')
    with open(meta_path,'rb') as f:
        meta=pickle.load(f)
    n_ch    = int(meta["n_ch"])
    n_rows  = int(meta["n_rows"])
    max_cols= int(meta["max_cols"])
    n_cols  = int(meta["n_cols"])
    waves=meta['waves']
    times=meta['times']
    other_params=meta['other_params']
    dtype   = (meta['dtype'])
    
    mm = np.memmap(data_path, dtype=dtype, mode="r", shape=(n_ch, n_rows, max_cols))
    spectra = mm[channel-1, :, :n_cols]     # view без копии

    return times,waves,spectra,other_params

def read_fbg_stream_raw_lp(filepath: str, debug: bool = False):
    """
    ПАТЧ:
      - читает до последнего корректного блока
      - при повреждённом/оборванном хвосте возвращает уже накопленные данные
      - если header повреждён/не дочитан -> выбрасывает понятное исключение
      - добавлен простой resync, если длина блока мусорная
    """
    import numpy as np
    import pickle
    import struct
    from typing import List, Dict

    MAX_BLOCK_LEN = 1 << 30  # 1GB
    RESYNC_WINDOW = 1024     # сколько байт максимум пытаемся сдвигаться при мусорной длине

    def _dbg(*a):
        if debug:
            print("[read_fbg_stream_raw_lp]", *a)

    with open(filepath, "rb") as f:
        # --- header ---
        try:
            header = pickle.load(f)
        except Exception as e:
            # Если header не дописан, извлечь данные корректно невозможно (неизвестны maps/геометрия)
            raise RuntimeError(f"Не удалось прочитать header (возможно файл оборван в заголовке): {e}") from e

        version = int(header.get("version", 4))

        # --- определяем карты выбора (0-based) ---
        if version >= 5 and ("channel_map" in header) and ("fbg_map" in header):
            channel_map = list(map(int, header["channel_map"]))
            fbg_map = [list(map(int, row)) for row in header["fbg_map"]]
            n_ch = len(channel_map)
            fbg_counts = [len(row) for row in fbg_map]

            if ("channel_list" in header) and ("FBGs_list" in header):
                channel_list = list(map(int, header["channel_list"]))
                FBGs_list = [list(map(int, row)) for row in header["FBGs_list"]]
            else:
                channel_list = [c + 1 for c in channel_map]
                FBGs_list = [[i + 1 for i in row] for row in fbg_map]
        else:
            channel_map = None
            fbg_map = None
            n_ch = int(header["channels"])
            fbg_per_ch = int(header["fbg_per_ch"])
            fbg_counts = [fbg_per_ch] * n_ch
            channel_list = list(range(1, n_ch + 1))
            FBGs_list = [list(range(1, fbg_per_ch + 1)) for _ in range(n_ch)]

        other_params = header.get("other_params", None)

        t_perf: List[float] = []
        acc: List[List[List[float]]] = [[[] for _ in range(fbg_counts[ch])] for ch in range(n_ch)]

        def _append_record(ts_p, wl):
            t_perf.append(float(ts_p))

            if not isinstance(wl, (list, tuple)):
                wl2 = []
            else:
                wl2 = list(wl)
            if len(wl2) != n_ch:
                wl2 = (wl2 + [[]] * n_ch)[:n_ch]

            for ch in range(n_ch):
                row = wl2[ch]
                need = fbg_counts[ch]
                cur = []
                if isinstance(row, (list, tuple)):
                    for x in row[:need]:
                        try:
                            cur.append(float(x))
                        except Exception:
                            cur.append(float("nan"))
                if len(cur) < need:
                    cur += [float("nan")] * (need - len(cur))
                for i in range(need):
                    acc[ch][i].append(cur[i])

        # --- основной цикл чтения блоков ---
        while True:
            len_buf = f.read(4)
            if not len_buf:
                break  # нормальный EOF
            if len(len_buf) < 4:
                break  # оборванная длина в конце

            try:
                (block_len,) = struct.unpack(">I", len_buf)
            except Exception:
                break  # мусор/обрыв

            if block_len <= 0 or block_len > (1 << 30):
                break  # мусорная длина => дальше читать нельзя

            blob = f.read(block_len)
            if len(blob) < block_len:
                break  # оборванный блок в конце

            try:
                batch = pickle.loads(blob)
            except Exception:
                break  # оборванный/битый pickle в конце

            for rec in batch:
                if isinstance(rec, tuple) and len(rec) == 4:
                    ts_p, ts_u, pkt_ctr, wl = rec
                    _append_record(ts_p, wl)

    # --- финализация ---
    t_perf_arr = np.asarray(t_perf, dtype=float)
    if t_perf_arr.size == 0:
        # Здесь теперь "пусто" будет только если реально не было ни одного корректного блока
        return t_perf_arr, {}, channel_list, FBGs_list, other_params

    t0 = t_perf_arr[0]
    times = t_perf_arr - t0

    channel_FBGs = [np.asarray(acc[ch], dtype=float) for ch in range(len(acc))]
    channels: Dict[int, Dict[int, np.ndarray]] = {}
    for i, ch in enumerate(channel_list):
        arr = channel_FBGs[i]
        fbgs = list(FBGs_list[i])
        channels[int(ch)] = {int(fbg_id): arr[j, :] for j, fbg_id in enumerate(fbgs)}

    return times, channels, channel_list, FBGs_list, other_params


class FrameFanout:
    """
    Один поток читает it.pop_freq_frame() и рассылает кадры всем подписчикам (очередям).
    Элемент кадра: (ts_perf: float, wl: List[List[float]])
    """
    def __init__(self, it: Any, idle_sleep: float = 0.0002):
        import threading
        self.it = it
        self.idle_sleep = float(idle_sleep)
        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None
        self._queues: List["Queue[Tuple[float, List[List[float]]]]"] = []
        self._lock = threading.Lock()

    def add_consumer_queue(self, q: "Queue[Tuple[float, List[List[float]]]]"):
        with self._lock:
            self._queues.append(q)
    def remove_consumer_queue(self, q):
        with self._lock:
            self._queues = [qq for qq in self._queues if qq is not q]
    def start(self):
        import threading, time
        if self._thr and self._thr.is_alive():
            return
        def _loop():
            while not self._stop.is_set():
                fr = self.it.pop_freq_frame()
                if fr is None:
                    time.sleep(self.idle_sleep)
                    continue
                wl = fr.get("wavelength_nm")
                if wl is None:
                    continue
                t = float(fr.get("t_perf", time.perf_counter()))
                with self._lock:
                    qs = list(self._queues)
                for q in qs:
                    try:
                        q.put_nowait((t, wl))
                    except Exception:
                        # Если очередь переполнена — пробуем отбросить старый и повторить
                        try:
                            _ = q.get_nowait()
                            q.put_nowait((t, wl))
                        except Exception:
                            pass
        self._thr = threading.Thread(target=_loop, name="FBG-Fanout", daemon=True)
        self._thr.start()

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        try:
            if self._thr:
                self._thr.join(timeout=timeout)
        except Exception:
            pass

# ==========================
# Live‑plot в реальном времени
# ==========================
# Исправление: record_and_plot должен сам закрывать окно графика и останавливать запись,
# когда прошло duration_sec.
#
# Сейчас у вас writer_thread сам заканчивается по t_end, но live_plot_wavelengths НЕ закрывается
# автоматически (blocking=False лишь делает show неблокирующим, окно остаётся жить).
#
# Решение: в record_and_plot запускаем таймер, который:
#   1) stop_event.set()  -> останавливает writer_thread
#   2) stop_plot()       -> закрывает окно matplotlib (plt.close(fig))
#   3) fan.stop()        -> останавливает fanout
#
# Важно: закрывать matplotlib-окно лучше из главного потока (GUI). Поэтому:
#   - Если есть активный backend (Qt/Tk), используем "GUI timer":
#       fig.canvas.new_timer(...)
#   - И как fallback — threading.Timer (может сработать, но GUI-бэкенды иногда ругаются)
def start_live_plot_session(it: Any,
                            plot_channels: List[int],          # 1-based
                            plot_FBGs: List[List[int]],        # 1-based (как в GUI params)
                            rep_rate_hz: float = 2000.0,
                            window_sec: float = 20.0,
                            max_fps: int = 30,
                            ylim: Optional[Tuple[float, float]] = None,
                            title_prefix: str = "Live",
                            use_subplots: bool = True,
                            queue_maxsize: int = 4000,
                            max_frames_per_update: int = 1200) -> Tuple[Callable[[], None], Dict[str, Any]]:
    """
    Запускает:
      - FrameFanout (единственный читатель it.pop_freq_frame)
      - по одной очереди на канал
      - по одному live_plot_wavelengths на канал

    plot_FBGs — 1-based индексы, как в вашем GUI (Params_it.FBGs)
    """
    from queue import Queue

    fan = FrameFanout(it, idle_sleep=0.0002)
    stops: List[Callable[[], None]] = []
    figs = []
    queues = []

    # создаём очереди и подписываем на fanout
    for ch1 in plot_channels:
        q = Queue(maxsize=int(queue_maxsize))
        fan.add_consumer_queue(q)
        queues.append(q)

    fan.start()

    for idx, ch1 in enumerate(plot_channels):
        fbg1_list = plot_FBGs[idx] if idx < len(plot_FBGs) else []
        fbg0 = [int(x) - 1 for x in fbg1_list]  # -> 0-based

        stop, fig = live_plot_wavelengths(
            it=it,
            channel=int(ch1),
            fbg_indices=fbg0,
            window_sec=float(window_sec),
            expected_rate_hz=float(rep_rate_hz),
            max_fps=int(max_fps),
            ylim=ylim,
            title=f"{title_prefix} (ch {ch1})",
            blocking=False,
            source_queue=queues[idx],
            use_subplots=use_subplots,
            max_frames_per_update=int(max_frames_per_update),
        )
        stops.append(stop)
        figs.append(fig)

    def stop_all():
        for s in stops:
            try:
                s()
            except Exception:
                pass
        try:
            fan.stop(timeout=1.0)
        except Exception:
            pass

    info = {
        "fanout": fan,
        "queues": queues,
        "figs": figs,
        "stops": stops,
    }
    return stop_all, info

def record_and_plot(it: Any,
                    filepath: str,
                    duration_sec: float,
                    channels: Optional[List[int]] = None,
                    FBGs: Optional[List[List[int]]] = None,
                    write_every_n: int = 1,
                    plot_channel: int = 1,
                    plot_fbg_indices: List[int] = (0, 1, 2),
                    # --- NEW ---
                    plot_channels: Optional[List[int]] = None,          # 1-based
                    plot_FBGs: Optional[List[List[int]]] = None,        # 0-based indices per channel (for plotting)
                    use_subplots: bool = True,
                    # -----------
                    window_sec: float = 10.0,
                    max_fps: int = 30,
                    ylim: Optional[Tuple[float, float]] = None,
                    title: Optional[str] = None,
                    other_params: Optional[Dict] = None
                    ) -> Tuple[Callable[[], None], Dict[str, Any]]:

    import gc
    import threading
    from queue import Queue, Empty
    import time

    FSYNC_EVERY_BATCHES = 20
    BATCH_SIZE = 1000
    WARMUP_SEC = 1.0
    DROP_DURING_WARMUP = True
    START_DELAY_SEC = 0.3
    DISABLE_GC_DURING_RECORD = True

    ch_map_0 = None
    fbg_map_0 = None
    if channels is not None:
        ch_map_0 = [int(c) - 1 for c in channels]
        if FBGs is not None:
            if len(FBGs) != len(ch_map_0):
                raise ValueError("Длина FBGs должна совпадать с длиной channels")
            fbg_map_0 = [[int(i) - 1 for i in arr] for arr in FBGs]

    if START_DELAY_SEC > 0:
        time.sleep(START_DELAY_SEC)

    q_rec: "Queue[Tuple[float, List[List[float]]]]" = Queue(maxsize=50000)
    q_plot: "Queue[Tuple[float, List[List[float]]]]" = Queue(maxsize=10000)

    fan = FrameFanout(it, idle_sleep=0.0002)
    fan.add_consumer_queue(q_rec)
    fan.add_consumer_queue(q_plot)
    fan.start()

    stats = {
        "started_at": time.perf_counter(),
        "wr_frames": 0,
        "wr_fps": 0.0,
        "blocks_written": 0,
    }

    stop_event = threading.Event()

    def writer_thread():
        nonlocal stats
        t_start = stats["started_at"]
        t_end = t_start + float(duration_sec)
        blocks_written = 0
        wr_count_since = 0
        last_wr = time.perf_counter()

        gc_was_enabled = gc.isenabled()
        if DISABLE_GC_DURING_RECORD and gc_was_enabled:
            gc.disable()

        write_every = max(1, int(write_every_n))
        taken_ctr = 0

        try:
            with open(filepath, "wb") as f:
                header = make_header(it, channel_map=ch_map_0, fbg_map=fbg_map_0, other_params=other_params)
                pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)

                batch: List[Tuple[float, float, int, List[List[float]]]] = []
                writing_active = False
                warmup_deadline = t_start + max(0.0, WARMUP_SEC)

                def flush_batch():
                    nonlocal blocks_written, batch
                    if not batch:
                        return
                    wrote = _write_block(f, batch)
                    if wrote:
                        blocks_written += 1
                        stats["blocks_written"] = blocks_written
                        batch.clear()
                        if FSYNC_EVERY_BATCHES and (blocks_written % FSYNC_EVERY_BATCHES == 0):
                            f.flush()
                            os.fsync(f.fileno())

                while not stop_event.is_set():
                    now = time.perf_counter()
                    if now >= t_end:
                        break

                    if not writing_active and now >= warmup_deadline:
                        writing_active = True

                    try:
                        t_perf, wl_full = q_rec.get(timeout=min(0.1, max(0.0, t_end - now)))
                    except Empty:
                        if writing_active:
                            flush_batch()
                        continue

                    if not writing_active and DROP_DURING_WARMUP:
                        continue

                    taken_ctr += 1
                    if (taken_ctr % write_every) != 0:
                        continue

                    if ch_map_0 is None:
                        wl_rows = [[float(x) for x in row] for row in wl_full]
                    else:
                        wl_rows = []
                        for idx, ch in enumerate(ch_map_0):
                            if 0 <= ch < len(wl_full):
                                src = wl_full[ch]
                                if fbg_map_0 is not None and idx < len(fbg_map_0):
                                    sel = fbg_map_0[idx]
                                    wl_rows.append([float(src[i]) if 0 <= i < len(src) else float("nan") for i in sel])
                                else:
                                    wl_rows.append([float(x) for x in src])
                            else:
                                wl_rows.append([])

                    ts_unix = time.time()
                    pkt_ctr = -1
                    batch.append((float(t_perf), float(ts_unix), int(pkt_ctr), wl_rows))

                    stats["wr_frames"] += 1
                    wr_count_since += 1

                    if writing_active and (len(batch) >= BATCH_SIZE):
                        flush_batch()

                    if (now - last_wr) >= 0.5:
                        stats["wr_fps"] = wr_count_since / (now - last_wr)
                        wr_count_since = 0
                        last_wr = now

                if writing_active:
                    flush_batch()
                f.flush()
                os.fsync(f.fileno())
        finally:
            if DISABLE_GC_DURING_RECORD and gc_was_enabled and not gc.isenabled():
                gc.enable()

    wr_thr = threading.Thread(target=writer_thread, name="FBG-Writer", daemon=True)
    wr_thr.start()

    # ---- Live plot ----
    # NEW: поддержка нескольких каналов (несколько фигур)
    if plot_channels is None:
        plot_channels = [int(plot_channel)]
    else:
        plot_channels = [int(c) for c in plot_channels]

    if plot_FBGs is None:
        plot_FBGs = [list(plot_fbg_indices) for _ in plot_channels]
    else:
        if len(plot_FBGs) != len(plot_channels):
            raise ValueError("Длина plot_FBGs должна совпадать с длиной plot_channels")
        plot_FBGs = [list(lst) for lst in plot_FBGs]

    stop_plots: List[Callable[[], None]] = []
    figs = []

    for k, ch1 in enumerate(plot_channels):
        ch_fbgs = plot_FBGs[k]
        stop_plot, fig = live_plot_wavelengths(
            it=it,
            channel=ch1,
            fbg_indices=ch_fbgs,
            window_sec=window_sec,
            max_fps=max_fps,
            ylim=ylim,
            title=(title or None),
            blocking=False,
            source_queue=q_plot,
            # --- NEW ---
            use_subplots=use_subplots
        )
        stop_plots.append(stop_plot)
        figs.append(fig)

    # ---- авто-остановка/авто-закрытие ----
    stop_called = threading.Event()

    def stop_all():
        if stop_called.is_set():
            return
        stop_called.set()

        stop_event.set()

        # закрыть все окна
        for sp in stop_plots:
            try:
                sp()
            except Exception:
                pass

        try:
            fan.stop(timeout=1.0)
        except Exception:
            pass

        try:
            wr_thr.join(timeout=2.0)
        except Exception:
            pass

        it.stop_freq_stream()

    # Таймер: достаточно повеситься на первую фигуру (или fallback, если нет фигур)
    try:
        fig0 = figs[0]
        timer = fig0.canvas.new_timer(interval=int(float(duration_sec) * 1000))
        timer.single_shot = True
        timer.add_callback(stop_all)
        timer.start()
    except Exception:
        threading.Timer(duration_sec, stop_all).start()

    return stop_all, stats

def live_plot_wavelengths(it,
                          channel: int,
                          fbg_indices,
                          window_sec: float = 20.0,
                          expected_rate_hz: float = 2000.0,
                          max_fps: int = 30,
                          title: Optional[str] = None,
                          ylim: Optional[Tuple[float, float]] = None,
                          blocking: bool = False,
                          source_queue=None,
                          use_subplots: bool = True,
                          # perf tuning
                          max_frames_per_update: int = 1200,
                          autoscale_every: int = 10):
    """
    ЕДИНСТВЕННАЯ актуальная версия live plot.

    ВАЖНО:
      - функция НЕ читает it.pop_freq_frame()
      - источник данных: source_queue с элементами (t_perf: float, wl_full: List[List[float]])
      - очередь должна наполняться из FrameFanout (один читатель pop_freq_frame на приложение)

    Это устраняет накопление задержки из-за конкурирующих читателей и даёт контролируемую нагрузку.

    channel: 1-based
    fbg_indices: 0-based индексы FBG внутри канала
    """
    import queue
    from collections import deque

    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    if source_queue is None:
        raise ValueError(
            "live_plot_wavelengths теперь работает только от source_queue. "
            "Создайте FrameFanout и передайте очередь."
        )

    q = source_queue

    # --- validate ---
    n_ch = int(getattr(it, "channels", 0) or 0)
    fbg_per_ch = int(getattr(it, "fbg_per_ch", 0) or 0)

    if n_ch > 0 and not (1 <= int(channel) <= n_ch):
        raise ValueError(f"Некорректный channel={channel}, допустимо 1..{n_ch}")
    channel_0 = int(channel) - 1

    fbg_indices = [int(i) for i in list(fbg_indices)]
    if fbg_per_ch > 0:
        for i in fbg_indices:
            if not (0 <= i < fbg_per_ch):
                raise ValueError(f"Некорректный индекс решётки {i}, допустимо 0..{fbg_per_ch-1}")

    # --- buffers ---
    maxlen = max(1000, int(float(expected_rate_hz) * float(window_sec) * 1.5))
    times = deque(maxlen=maxlen)
    series = {i: deque(maxlen=maxlen) for i in fbg_indices}

    plt.ion()

    # --- figure ---
    if use_subplots:
        n = max(1, len(fbg_indices))
        fig_h = max(3.0, 1.6 * n)
        fig, axes = plt.subplots(n, 1, sharex=True, figsize=(9, fig_h))
        axes_list = [axes] if n == 1 else list(np.ravel(axes))

        fig.suptitle(title or f"Channel {channel}")
        fig.supxlabel("Time, s")
        fig.supylabel("FBG wavelength, nm")

        colors = plt.cm.tab10.colors
        lines = {}  # fbg -> (line, ax)

        for k, fbg in enumerate(fbg_indices):
            axk = axes_list[k]
            axk.grid(True, alpha=0.25)
            axk.set_title(f"FBG {fbg + 1}", loc="left", fontsize=10, pad=2)
            line, = axk.plot([], [], color=colors[k % len(colors)], lw=1.2)
            if ylim is not None:
                axk.set_ylim(*ylim)
            lines[fbg] = (line, axk)

        fig.tight_layout(rect=(0.06, 0.06, 1.0, 0.95))

    else:
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.set_title(title or f"Channel {channel}")
        ax.set_xlabel("Time, s")
        ax.set_ylabel("FBG wavelength, nm")
        ax.grid(True, alpha=0.25)

        colors = plt.cm.tab10.colors
        lines = {}  # fbg -> line
        for k, fbg in enumerate(fbg_indices):
            line, = ax.plot([], [], label=f"FBG {fbg + 1}", color=colors[k % len(colors)], lw=1.2)
            lines[fbg] = line
        ax.legend(loc="best")
        if ylim is not None:
            ax.set_ylim(*ylim)

    t0 = None
    update_counter = 0

    def _consume_batch():
        """Съесть пачку кадров, чтобы сохранить детализацию, но не уходить в вечный догон."""
        nonlocal t0
        got = 0
        for _ in range(int(max_frames_per_update)):
            try:
                t, wl = q.get_nowait()
            except queue.Empty:
                break

            if t0 is None:
                t0 = float(t)

            times.append(float(t))

            row_ch = []
            if isinstance(wl, (list, tuple)) and len(wl) > channel_0:
                row_ch = wl[channel_0]

            for i in fbg_indices:
                val = float("nan")
                if isinstance(row_ch, (list, tuple)) and len(row_ch) > i:
                    try:
                        val = float(row_ch[i])
                    except Exception:
                        val = float("nan")
                series[i].append(val)

            got += 1
        return got

    def update(_):
        nonlocal update_counter
        update_counter += 1

        _consume_batch()

        if t0 is None or len(times) == 0:
            if use_subplots:
                return [lines[i][0] for i in fbg_indices]
            return list(lines.values())

        t_arr = np.fromiter(times, dtype=float, count=len(times))
        t_rel = t_arr - t0
        t_now = float(t_rel[-1])
        t_min = max(0.0, t_now - float(window_sec))

        if use_subplots:
            for i in fbg_indices:
                y = np.fromiter(series[i], dtype=float, count=len(series[i]))
                line, ax_i = lines[i]

                m = min(y.size, t_rel.size)
                x_plot = t_rel[-m:]
                y_plot = y[-m:]

                if window_sec > 0:
                    msk = x_plot >= t_min
                    x_plot = x_plot[msk]
                    y_plot = y_plot[msk]

                line.set_data(x_plot, y_plot)
                ax_i.set_xlim(t_min, max(float(window_sec), t_now))

                if ylim is None and (update_counter % int(autoscale_every) == 0):
                    ax_i.relim()
                    ax_i.autoscale_view(scalex=False, scaley=True)

            return [lines[i][0] for i in fbg_indices]

        else:
            for i, line in lines.items():
                y = np.fromiter(series[i], dtype=float, count=len(series[i]))
                m = min(y.size, t_rel.size)
                x_plot = t_rel[-m:]
                y_plot = y[-m:]
                if window_sec > 0:
                    msk = x_plot >= t_min
                    x_plot = x_plot[msk]
                    y_plot = y_plot[msk]
                line.set_data(x_plot, y_plot)

            ax.set_xlim(t_min, max(float(window_sec), t_now))
            if ylim is None and (update_counter % int(autoscale_every) == 0):
                ax.relim()
                ax.autoscale_view(scalex=False, scaley=True)
            return list(lines.values())

    interval_ms = max(1, int(1000 / max(1, int(max_fps))))
    ani = FuncAnimation(fig, update, interval=interval_ms, blit=False,
                        cache_frame_data=False, save_count=1000)
    fig._live_anim_ref = ani

    def stop():
        try:
            ani.event_source.stop()
        except Exception:
            pass
        try:
            import matplotlib.pyplot as _plt
            _plt.close(fig)
        except Exception:
            pass

    plt.show(block=blocking)
    if not blocking:
        plt.pause(0.05)

    return stop, fig
def safe_stop_interrogator(it: Any, join_timeout: float = 2.0) -> None:
    """
    Аккуратно останавливает и очищает интеррогатор:
      - stop_freq_stream()
      - ставит _rx_stop, закрывает сокет, join RX-поток
      - чистит ring и callbacks
    Вызывайте между повторами записи для «чистого» состояния.
    """
    try:
        it.stop_freq_stream()
    except Exception:
        pass

    try:
        if hasattr(it, "_rx_stop"):
            it._rx_stop.set()
    except Exception:
        pass

    try:
        if getattr(it, "_sock", None):
            it._sock.close()
            it._sock = None
    except Exception:
        pass

    try:
        if hasattr(it, "_rx_thread") and it._rx_thread and it._rx_thread.is_alive():
            it._rx_thread.join(timeout=join_timeout)
    except Exception:
        pass

    try:
        if hasattr(it, "_ring"):
            it._ring.clear()
    except Exception:
        pass
    try:
        if hasattr(it, "_callbacks"):
            it._callbacks.clear()
    except Exception:
        pass


def record_long_dynamics(it,
                         filepath: str,
                         duration_sec: float,
                         time_step: float = 1.0,
                         channels: Optional[List[int]] = None,        # 1-based (optional)
                         FBGs: Optional[List[List[int]]] = None,      # 1-based (optional)
                         batch_size: int = 1,
                         other_params: Optional[Dict] = None,
                         timeout_single: float = 0.2,
                         init_timeout_sec: float = 10.0,
                         append: bool = False) -> Dict[str, Any]:
    """
    Долгая запись редких измерений через it.get_single_FBG_measurement() в файл формата
    record_to_file/read_fbg_stream_raw_lp, причём header содержит только непустые каналы/решётки.

    ВАЖНОЕ ИЗМЕНЕНИЕ ПО ПРОСЬБЕ:
      - файл открывается и закрывается на КАЖДУЮ запись блока (batch),
        чтобы можно было читать/копировать файл во время работы.
      - это заметно медленнее, но при time_step~1с обычно нормально.

    append=False:
      создаёт новый файл и пишет header.
    append=True:
      дописывает в существующий файл (header не пишется).
      (При append предполагается, что header уже совместим с текущей геометрией.)
    """
    import os
    import time
    import struct
    import pickle
    import math

    if time_step <= 0:
        raise ValueError("time_step должен быть > 0")
    if duration_sec <= 0:
        raise ValueError("duration_sec должен быть > 0")
    if batch_size <= 0:
        raise ValueError("batch_size должен быть > 0")

    orig_channels = int(getattr(it, "channels", 0) or 0)
    orig_fbg = int(getattr(it, "fbg_per_ch", 0) or 0)
    if orig_channels <= 0 or orig_fbg <= 0:
        raise RuntimeError("Неизвестны it.channels/it.fbg_per_ch (нужно прочитать параметры модуля)")

    # ----------------------------
    # 1) Пользовательский выбор (как в record_to_file)
    # ----------------------------
    ch_map_0: Optional[List[int]] = None
    fbg_map_0: Optional[List[List[int]]] = None

    if channels is not None:
        if not isinstance(channels, (list, tuple)) or len(channels) == 0:
            raise ValueError("channels должен быть непустым списком (1-based)")
        ch_map_0 = [int(ch) - 1 for ch in channels]
        for c0 in ch_map_0:
            if not (0 <= c0 < orig_channels):
                raise ValueError(f"Некорректный канал {c0 + 1}, допустимо 1..{orig_channels}")

        if FBGs is not None:
            if len(FBGs) != len(ch_map_0):
                raise ValueError("Длина FBGs должна совпадать с длиной channels")
            fbg_map_0 = []
            for lst in FBGs:
                if not isinstance(lst, (list, tuple)) or len(lst) == 0:
                    raise ValueError("Каждый элемент FBGs должен быть непустым списком (1-based)")
                row0 = [int(i) - 1 for i in lst]
                for i0 in row0:
                    if not (0 <= i0 < orig_fbg):
                        raise ValueError(f"Некорректный FBG индекс {i0 + 1}, допустимо 1..{orig_fbg}")
                fbg_map_0.append(row0)
        else:
            fbg_map_0 = None

    def _is_valid_float(x) -> bool:
        try:
            xf = float(x)
            return not math.isnan(xf)
        except Exception:
            return False

    def _row_to_float_list(row) -> List[float]:
        if not isinstance(row, (list, tuple)):
            try:
                row = list(row)
            except Exception:
                return []
        out = []
        for x in row:
            try:
                out.append(float(x))
            except Exception:
                out.append(float("nan"))
        return out

    def _select_from_meas(meas, ch_map0, fbg_map0) -> List[List[float]]:
        """
        Применяет выбор каналов/FBG по индексам.
        Если maps None -> возвращает meas как есть (но приведённый к float-спискам).
        """
        if not isinstance(meas, (list, tuple)):
            meas = []
        meas = [_row_to_float_list(r) for r in meas]

        if ch_map0 is None:
            return meas

        wl_rows: List[List[float]] = []
        for idx, ch0 in enumerate(ch_map0):
            src = meas[ch0] if 0 <= ch0 < len(meas) else []
            if fbg_map0 is not None and idx < len(fbg_map0):
                sel = fbg_map0[idx]
                wl_rows.append([src[i] if 0 <= i < len(src) else float("nan") for i in sel])
            else:
                wl_rows.append(list(src))
        return wl_rows

    # ----------------------------
    # 2) Если выбор не задан — определяем непустые каналы/FBG по первому измерению
    # ----------------------------
    if ch_map_0 is None and not append:
        # В режиме append лучше НЕ пытаться переопределять геометрию:
        # предполагаем, что файл/заголовок уже есть.
        t_init0 = time.perf_counter()
        meas0 = None
        while (time.perf_counter() - t_init0) < float(init_timeout_sec):
            try:
                m = it.get_single_FBG_measurement(timeout=timeout_single)
                if isinstance(m, (list, tuple)) and len(m) > 0:
                    meas0 = [_row_to_float_list(r) for r in m]
                    break
            except Exception:
                time.sleep(0.05)

        if meas0 is None:
            raise RuntimeError("Не удалось получить initial single measurement для определения непустых каналов/FBG")

        ch_map_0 = []
        fbg_map_0 = []
        for ch0 in range(min(orig_channels, len(meas0))):
            row = meas0[ch0]
            idxs = [i for i, x in enumerate(row) if _is_valid_float(x)]
            if len(idxs) == 0:
                continue
            ch_map_0.append(ch0)
            fbg_map_0.append(idxs)

        if len(ch_map_0) == 0:
            raise RuntimeError("В initial measurement нет ни одного непустого канала/решётки (все NaN/пусто)")

    # ----------------------------
    # 3) Header + "форма" кадра
    # ----------------------------
    if append:
        # append-mode: не пишем header, но форма нужна.
        # В идеале её нужно читать из файла, но ваш reader ожидает header в начале.
        # Поэтому здесь требуем, чтобы channels/FBGs были заданы явно.
        if ch_map_0 is None:
            raise ValueError("append=True требует явного задания channels (и желательно FBGs), чтобы сохранить геометрию")
    else:
        header = make_header(it, channel_map=ch_map_0, fbg_map=fbg_map_0, other_params=other_params)

        # Создаём/пересоздаём файл и пишем header (открыли-закрыли сразу)
        with open(filepath, "wb") as f:
            pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())

    if fbg_map_0 is None:
        fbg_counts = [orig_fbg] * len(ch_map_0)
    else:
        fbg_counts = [len(r) for r in fbg_map_0]

    def _shape_to_header(wl_rows: List[List[float]]) -> List[List[float]]:
        out: List[List[float]] = []
        n_ch = len(ch_map_0 or [])
        wl_rows = list(wl_rows) if isinstance(wl_rows, (list, tuple)) else []
        if len(wl_rows) != n_ch:
            wl_rows = (wl_rows + [[]] * n_ch)[:n_ch]

        for ch in range(n_ch):
            row = wl_rows[ch]
            need = fbg_counts[ch]
            cur = []
            if isinstance(row, (list, tuple)):
                cur = [float(x) for x in row[:need]]
            if len(cur) < need:
                cur = cur + [float("nan")] * (need - len(cur))
            out.append(cur)
        return out

    # ----------------------------
    # 4) Основной цикл: накапливаем batch и дописываем его в файл,
    #    открывая/закрывая файл на КАЖДЫЙ блок.
    # ----------------------------
    st = {
        "filepath": filepath,
        "duration_sec": float(duration_sec),
        "time_step": float(time_step),
        "wr_frames": 0,
        "blocks_written": 0,
        "errors": 0,
        "append": bool(append),
    }

    t0_perf = time.perf_counter()
    t_end = t0_perf + float(duration_sec)
    next_tick = t0_perf

    batch: List[Tuple[float, float, int, List[List[float]]]] = []
    blocks_written = 0

    def _append_batch_to_file(batch_to_write):
        nonlocal blocks_written
        if not batch_to_write:
            return
        blob = pickle.dumps(batch_to_write, protocol=pickle.HIGHEST_PROTOCOL)
        payload = struct.pack(">I", len(blob)) + blob

        with open(filepath, "ab") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        blocks_written += 1
        st["blocks_written"] = blocks_written

    while True:
        now = time.perf_counter()
        if now >= t_end:
            break

        if now < next_tick:
            time.sleep(min(0.2, next_tick - now))
            continue

        k = math.floor((now - t0_perf) / time_step) + 1
        next_tick = t0_perf + k * time_step

        try:
            meas = it.get_single_FBG_measurement(timeout=timeout_single)
            wl_sel = _select_from_meas(meas, ch_map_0, fbg_map_0)
            wl_sel = _shape_to_header(wl_sel)

            ts_perf = time.perf_counter()
            ts_unix = time.time()
            pkt_ctr = -1
            print('{:.2f} s '.format(ts_perf-t0_perf))
            batch.append((float(ts_perf), float(ts_unix), int(pkt_ctr), wl_sel))
            st["wr_frames"] += 1

        except Exception:
            st["errors"] += 1
            continue

        if len(batch) >= batch_size:
            _append_batch_to_file(batch)
            batch.clear()

    if batch:
        _append_batch_to_file(batch)
        batch.clear()

    return st


from typing import Any, Dict, List, Optional, Tuple

def record_to_file_from_queue(it: Any,
                              q_rec: "Queue[Tuple[float, List[List[float]]]]",
                              filepath: str,
                              duration_sec: float,
                              channels: Optional[List[int]] = None,        # 1-based
                              FBGs: Optional[List[List[int]]] = None,      # 1-based
                              write_every_n: int = 1,
                              other_params: Optional[Dict] = None,
                              warmup_sec: float = 1.0,
                              drop_during_warmup: bool = True,
                              batch_size: int = 1000,
                              fsync_every_batches: int = 20) -> Dict[str, Any]:
    """
    Пишет .fbgs из очереди кадров q_rec, которую наполняет FrameFanout.
    Элемент очереди: (t_perf, wl_full), где wl_full: List[List[float]] по всем каналам.
    """

    # --- maps 0-based для заголовка и выбора данных ---
    ch_map_0 = None
    fbg_map_0 = None
    if channels is not None:
        ch_map_0 = [int(c) - 1 for c in channels]
        if FBGs is not None:
            if len(FBGs) != len(ch_map_0):
                raise ValueError("Длина FBGs должна совпадать с длиной channels")
            fbg_map_0 = [[int(i) - 1 for i in arr] for arr in FBGs]

    stats = {
        "started_at": time.perf_counter(),
        "wr_frames": 0,
        "wr_fps": 0.0,
        "blocks_written": 0,
    }

    t_start = stats["started_at"]
    t_end = t_start + float(duration_sec)
    warmup_deadline = t_start + max(0.0, float(warmup_sec))

    write_every = max(1, int(write_every_n))
    taken_ctr = 0

    def _write_block(fh, batch):
        if not batch:
            return 0
        blob = pickle.dumps(batch, protocol=pickle.HIGHEST_PROTOCOL)
        fh.write(struct.pack(">I", len(blob)))
        fh.write(blob)
        return 1

    blocks_written = 0
    wr_count_since = 0
    last_wr = time.perf_counter()

    with open(filepath, "wb") as f:
        header = make_header(it, channel_map=ch_map_0, fbg_map=fbg_map_0, other_params=other_params)
        pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)

        batch: List[Tuple[float, float, int, List[List[float]]]] = []
        writing_active = False

        def flush_batch():
            nonlocal blocks_written, batch
            wrote = _write_block(f, batch)
            if wrote:
                blocks_written += 1
                stats["blocks_written"] = blocks_written
                batch.clear()
                if fsync_every_batches and (blocks_written % fsync_every_batches == 0):
                    f.flush()
                    os.fsync(f.fileno())

        while True:
            now = time.perf_counter()
            if now >= t_end:
                break

            if not writing_active and now >= warmup_deadline:
                writing_active = True

            try:
                t_perf, wl_full = q_rec.get(timeout=min(0.1, max(0.0, t_end - now)))
            except Empty:
                if writing_active:
                    flush_batch()
                continue

            if not writing_active and drop_during_warmup:
                continue

            taken_ctr += 1
            if (taken_ctr % write_every) != 0:
                continue

            # --- применяем выбор каналов/FBG ---
            if ch_map_0 is None:
                wl_rows = [[float(x) for x in row] for row in wl_full]
            else:
                wl_rows = []
                for idx, ch0 in enumerate(ch_map_0):
                    src = wl_full[ch0] if (isinstance(wl_full, (list, tuple)) and 0 <= ch0 < len(wl_full)) else []
                    if fbg_map_0 is not None and idx < len(fbg_map_0):
                        sel = fbg_map_0[idx]
                        wl_rows.append([float(src[i]) if 0 <= i < len(src) else float("nan") for i in sel])
                    else:
                        wl_rows.append([float(x) for x in src])

            ts_unix = time.time()
            pkt_ctr = -1
            batch.append((float(t_perf), float(ts_unix), int(pkt_ctr), wl_rows))
            stats["wr_frames"] += 1
            wr_count_since += 1

            if writing_active and (len(batch) >= batch_size):
                flush_batch()

            if (now - last_wr) >= 0.5:
                stats["wr_fps"] = wr_count_since / (now - last_wr)
                wr_count_since = 0
                last_wr = now

        if writing_active:
            flush_batch()

        f.flush()
        os.fsync(f.fileno())

    return stats
# ==========================
# Пример использования (комментарии)
# ==========================
# from FBGrecorder import record_to_file, read_fbg_stream_raw_lp, safe_stop_interrogator, live_plot_wavelengths
#
# # 1) создать и запустить интеррогатор
# it = InterrogatorUDP(cfg)
# it.start_freq_stream()
#
# # 2) запись 10 секунд
# stats = record_to_file(it, "fbg_dump.pkl", duration_sec=10.0)
# print("Запись завершена:", stats)
#
# # 3) чтение
# times, channels = read_fbg_stream_raw_lp("fbg_dump.pkl")
# print("samples:", times.size, "channels:", len(channels), "shape ch0:", channels[0].shape)
#
# # 4) live-плот (запускать из главного GUI-потока)
# stop_live = live_plot_wavelengths(it, channel=0, fbg_indices=[0, 1, 5], window_sec=10.0, max_fps=30)
# # ... когда нужно остановить:
# # stop_live()
#
# # 5) безопасная остановка перед повторным запуском
# safe_stop_interrogator(it)