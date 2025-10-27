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

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from queue import Queue, Empty, Full
from typing import Any, Callable, Dict, List, Optional, Tuple

# Мягкие зависимости
try:
    import pickle
except Exception as e:
    raise RuntimeError("pickle недоступен") from e


# ==========================
# Конфигурации и статистика
# ==========================

from typing import Optional

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
    record_channel: Optional[int] = None


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

def make_header(it: Any, channel_map: Optional[List[int]] = None) -> Dict[str, Any]:
    hdr = {
        "channels": int(getattr(it, "channels", 0)),
        "fbg_per_ch": int(getattr(it, "fbg_per_ch", 0)),
        "version": 4,
        "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_ch][fbg])",
    }
    if channel_map is not None:
        hdr["original_channels"] = int(getattr(it, "channels", 0))
        hdr["channel_map"] = list(map(int, channel_map))
        hdr["channels"] = len(channel_map)
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
                    # пустой или неожиданный формат — пропустим
                    continue
    
                # Фильтр по каналу (если включён)
                if self.cfg.record_channel is not None:
                    ch = int(self.cfg.record_channel)
                    if not (0 <= ch < len(wl)):
                        # устройство дало меньше каналов — пропустим кадр
                        continue
                    wl = [wl[ch]]
    
                # Привести WL к списку списков float
                compact = []
                for row in wl:
                    if not isinstance(row, (list, tuple)):
                        # иногда может прийти np.ndarray — приведём
                        try:
                            row = list(row)
                        except Exception:
                            row = []
                    compact.append([float(x) for x in row])
    
                ts_perf = float(fr.get("t_perf", time.perf_counter()))
                ts_unix = float(fr.get("timestamp", time.time()))
                pkt_ctr = int(fr.get("pkt_counter_be32", -1))
    
                rec = (ts_perf, ts_unix, pkt_ctr, compact)
    
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

        def warmup_done(now: float) -> bool:
            if now >= warmup_deadline:
                return True
            if self.cfg.min_rate_hz > 0.0:
                return self._stats.rx_fps >= self.cfg.min_rate_hz
            return False

        try:
            with open(self.cfg.filepath, "wb") as f:
                ch_map = None
                if self.cfg.record_channel is not None:
                    ch_map = [int(self.cfg.record_channel)]
                header = make_header(self.it, channel_map=ch_map)
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

                    # Активная запись
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
                   batch_size: int = 1000,
                   queue_max: int = 50000,
                   fsync_every_batches: int = 20,
                   idle_sleep_empty_ring: float = 0.0002,
                   start_delay_sec: float = 0.5,
                   warmup_sec: float = 1.5,
                   drop_during_warmup: bool = True,
                   min_rate_hz: float = 1500.0,
                   rate_window_sec: float = 0.8,
                   disable_gc_during_record: bool = True,
                   record_channel: Optional[int] = None  # <-- новый аргумент
                   ) -> Dict[str, Any]:
  
    """
    Удобная обёртка: старт записи, ожидание завершения, остановка. Возвращает финальную статистику.
    """
    # Закроем GUI и вычистим мусор перед записью
    configure_headless_matplotlib()
    import gc as _gc
    _gc.collect()

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
        record_channel=record_channel,  # <-- сюда
    )

    rec = FBGRecorder(it, cfg)
    rec.start()
    rec.wait_done(timeout=duration_sec + 10.0)
    rec.stop()

    return rec.stats()


# ==========================
# Чтение записанного файла
# ==========================
def read_fbg_stream_raw_lp(filepath: str):
    import numpy as np
    with open(filepath, "rb") as f:
        header = pickle.load(f)

        # Поддержка channel_map (если писали только часть каналов)
        channel_map = header.get("channel_map", None)
        if channel_map is not None:
            n_ch = int(header.get("channels", len(channel_map)))
        else:
            n_ch = int(header["channels"])

        fbg_per_ch = int(header["fbg_per_ch"])

        t_perf: List[float] = []
        acc: List[List[List[float]]] = [[[] for _ in range(fbg_per_ch)] for _ in range(n_ch)]

        while True:
            len_buf = f.read(4)
            if not len_buf or len(len_buf) < 4:
                break
            (block_len,) = struct.unpack(">I", len_buf)
            if block_len <= 0 or block_len > (1 << 30):
                break
            blob = f.read(block_len)
            if len(blob) < block_len:
                break

            try:
                batch = pickle.loads(blob)
            except Exception:
                break

            for rec in batch:
                if not (isinstance(rec, tuple) and len(rec) == 4):
                    continue
                ts_p, ts_u, pkt_ctr, wl = rec
                t_perf.append(float(ts_p))

                # wl должен быть длиной n_ch (в т.ч. 1 при записи одного канала)
                if len(wl) != n_ch:
                    wl = (wl + [[]] * n_ch)[:n_ch]

                for ch in range(n_ch):
                    row = wl[ch]
                    if len(row) < fbg_per_ch:
                        row = row + [float("nan")] * (fbg_per_ch - len(row))
                    elif len(row) > fbg_per_ch:
                        row = row[:fbg_per_ch]
                    for i in range(fbg_per_ch):
                        acc[ch][i].append(float(row[i]))

    t_perf_arr = np.asarray(t_perf, dtype=float)
    if t_perf_arr.size == 0:
        return t_perf_arr, []

    t0 = t_perf_arr[0]
    times = t_perf_arr - t0
    channels = [np.asarray(acc[ch], dtype=float) for ch in range(n_ch)]
    return times, channels

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
def record_and_plot(it: Any,
                    filepath: str,
                    duration_sec: float,
                    # запись
                    batch_size: int = 1000,
                    fsync_every_batches: int = 20,
                    warmup_sec: float = 1.0,
                    drop_during_warmup: bool = True,
                    start_delay_sec: float = 0.3,
                    disable_gc_during_record: bool = True,
                    # live-плот
                    plot_channel: int = 0,
                    plot_fbg_indices: List[int] = (0, 1, 2),
                    window_sec: float = 10.0,
                    max_fps: int = 30,
                    ylim: Optional[Tuple[float, float]] = None,
                    title: Optional[str] = None,
                    ) -> Tuple[Callable[[], None], Dict[str, Any]]:
    """
    Одновременные запись в файл и live-отрисовка.
    Возвращает: stop_all(), stats_dict.
    """
    import gc
    import threading            # <-- импорт раньше первого использования
    from queue import Queue, Empty
    import time

    # Небольшая задержка для старта потока устройства
    if start_delay_sec > 0:
        time.sleep(start_delay_sec)

    # Очереди для писателя и для графика
    q_rec: "Queue[Tuple[float, List[List[float]]]]" = Queue(maxsize=50000)
    q_plot: "Queue[Tuple[float, List[List[float]]]]" = Queue(maxsize=10000)

    # Fanout
    fan = FrameFanout(it, idle_sleep=0.0002)
    fan.add_consumer_queue(q_rec)
    fan.add_consumer_queue(q_plot)
    fan.start()

    # Статистика записи
    stats = {
        "started_at": time.perf_counter(),
        "wr_frames": 0,
        "wr_fps": 0.0,
        "blocks_written": 0,
    }

    # Писатель батчами (берёт кадры из q_rec)
    stop_event = threading.Event()

    def writer_thread():
        t_start = stats["started_at"]
        t_end = t_start + float(duration_sec)
        blocks_written = 0
        wr_count_since = 0
        last_wr = time.perf_counter()

        gc_was_enabled = gc.isenabled()
        if disable_gc_during_record and gc_was_enabled:
            gc.disable()

        try:
            with open(filepath, "wb") as f:
                header = make_header(it)
                pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)

                batch: List[Tuple[float, float, int, List[List[float]]]] = []
                writing_active = False
                warmup_deadline = t_start + max(0.0, warmup_sec)

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

                while not stop_event.is_set():
                    now = time.perf_counter()
                    if now >= t_end:
                        break

                    if not writing_active and now >= warmup_deadline:
                        writing_active = True

                    try:
                        t_perf, wl = q_rec.get(timeout=min(0.1, max(0.0, t_end - now)))
                    except Empty:
                        if writing_active:
                            flush_batch()
                        continue

                    # surrogate поля (если нужно — можно прокинуть реальные через fanout)
                    ts_unix = time.time()
                    pkt_ctr = -1
                    compact = [[float(x) for x in row] for row in wl]
                    rec = (float(t_perf), float(ts_unix), int(pkt_ctr), compact)

                    if not writing_active and drop_during_warmup:
                        continue

                    batch.append(rec)
                    stats["wr_frames"] += 1
                    wr_count_since += 1

                    if writing_active and (len(batch) >= batch_size):
                        flush_batch()

                    if (now - last_wr) >= 0.5:
                        stats["wr_fps"] = wr_count_since / (now - last_wr)
                        wr_count_since = 0
                        last_wr = now

                # финальный сброс
                if writing_active:
                    flush_batch()
                f.flush()
                os.fsync(f.fileno())
        finally:
            if disable_gc_during_record and gc_was_enabled and not gc.isenabled():
                gc.enable()

    wr_thr = threading.Thread(target=writer_thread, name="FBG-Writer", daemon=True)
    wr_thr.start()

    # Live-плот читает из q_plot
    stop_plot = live_plot_wavelengths(
        it=it,
        channel=plot_channel,
        fbg_indices=list(plot_fbg_indices),
        window_sec=window_sec,
        max_fps=max_fps,
        ylim=ylim,
        title=title,
        blocking=False,
        source_queue=q_plot
    )

    def stop_all():
        # Закрыть окно графика
        try:
            stop_plot()
        except Exception:
            pass
        # Остановить writer
        stop_event.set()
        try:
            wr_thr.join(timeout=2.0)
        except Exception:
            pass
        # Остановить fanout
        try:
            fan.stop(timeout=1.0)
        except Exception:
            pass

    return stop_all, stats

def live_plot_wavelengths(it,
                          channel: int,
                          fbg_indices,
                          window_sec: float = 10.0,
                          expected_rate_hz: float = 2000.0,
                          max_fps: int = 30,
                          title: Optional[str] = None,
                          ylim: Optional[Tuple[float, float]] = None,
                          blocking: bool = False,
                          source_queue: "Queue[Tuple[float, List[List[float]]]]" = None):
    """
    Реального времени график длин волн выбранных решёток одного канала.

    Если source_queue передан — функция НЕ создаёт свой RX-поток, а читает кадры из внешней очереди.
    Элемент очереди: (t_perf: float, wl: List[List[float]]).

    Важно:
      - Запускать из главного GUI-потока.
      - Нужен интерактивный backend (Qt5Agg/TkAgg).
    """
    import threading
    import queue
    import time
    from collections import deque

    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    # Валидация индексов
    n_ch = int(getattr(it, "channels", 0) or 0)
    fbg_per_ch = int(getattr(it, "fbg_per_ch", 0) or 0)
    if n_ch > 0 and not (0 <= channel < n_ch):
        raise ValueError(f"Некорректный channel={channel}, допустимо 0..{n_ch-1}")

    fbg_indices = list(fbg_indices)
    if fbg_per_ch > 0:
        for i in fbg_indices:
            if not (0 <= i < fbg_per_ch):
                raise ValueError(f"Некорректный индекс решётки {i}, допустимо 0..{fbg_per_ch-1}")

    # Локальная очередь, если внешний источник не задан
    own_queue = False
    if source_queue is None:
        q = queue.Queue(maxsize=10000)
        own_queue = True
    else:
        q = source_queue

    stop_event = threading.Event()

    maxlen = max(1000, int(expected_rate_hz * window_sec * 1.5))
    times = deque(maxlen=maxlen)
    series = {i: deque(maxlen=maxlen) for i in fbg_indices}

    # Если нет внешнего источника — запустим свой легкий RX-поток
    def worker():
        while not stop_event.is_set():
            fr = it.pop_freq_frame()
            if fr is None:
                time.sleep(0.0005)
                continue
            wl = fr.get("wavelength_nm")
            if wl is None or len(wl) <= channel:
                continue
            t = float(fr.get("t_perf", time.perf_counter()))
            try:
                q.put_nowait((t, wl))
            except queue.Full:
                try:
                    _ = q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait((t, wl))
                except queue.Full:
                    pass

    if own_queue:
        rx_thread = threading.Thread(target=worker, daemon=True)
        rx_thread.start()
    else:
        rx_thread = None

    import matplotlib.pyplot as plt
    plt.ion()
    fig, ax = plt.subplots(figsize=(8, 4))
    title = title or f"Channel {channel} — FBG {fbg_indices}"
    ax.set_title(title)
    ax.set_xlabel("Time, s")
    ax.set_ylabel("FBG wavelength, nm")

    lines = {}
    colors = plt.cm.tab10.colors
    for k, i in enumerate(fbg_indices):
        line, = ax.plot([], [], label=f"FBG {i}", color=colors[k % len(colors)])
        lines[i] = line
    ax.legend(loc="best")

    if ylim is not None:
        ax.set_ylim(*ylim)

    t0 = None

    def update(_frame_idx):
        nonlocal t0
        # забирать всё накопившееся
        while True:
            try:
                t, wl = q.get_nowait()
            except queue.Empty:
                break
            if t0 is None:
                t0 = t
            times.append(t)

            row_ch = wl[channel] if isinstance(wl, (list, tuple)) and len(wl) > channel else []
            for i in fbg_indices:
                val = float("nan")
                if isinstance(row_ch, (list, tuple)) and len(row_ch) > i:
                    val = float(row_ch[i])
                series[i].append(val)

        if t0 is None or len(times) == 0:
            return list(lines.values())

        import numpy as np
        t_arr = np.asarray(times, dtype=float)
        t_rel = t_arr - t0
        t_now = t_rel[-1]
        mask = t_rel >= max(0.0, t_now - window_sec)

        for i, line in lines.items():
            y = np.asarray(series[i], dtype=float)
            if y.size != t_rel.size:
                m = min(len(y), len(t_rel))
                x_plot = t_rel[-m:]
                y_plot = y[-m:]
                if window_sec > 0:
                    mask_m = x_plot >= max(0.0, x_plot[-1] - window_sec)
                    x_plot = x_plot[mask_m]
                    y_plot = y_plot[mask_m]
            else:
                x_plot = t_rel[mask]
                y_plot = y[mask]
            line.set_data(x_plot, y_plot)

        ax.set_xlim(max(0.0, t_now - window_sec), max(window_sec, t_now))
        if ylim is None:
            ax.relim()
            ax.autoscale_view(scalex=False, scaley=True)

        return list(lines.values())

    from matplotlib.animation import FuncAnimation
    interval_ms = max(1, int(1000 / max_fps))
    ani = FuncAnimation(fig, update, interval=interval_ms, blit=False,
                        cache_frame_data=False, save_count=1000)
    fig._live_anim_ref = ani  # сильная ссылка, чтобы GC не удалил анимацию

    def _on_close(event=None):
        stop_event.set()
        try:
            if rx_thread is not None:
                rx_thread.join(timeout=1.0)
        except Exception:
            pass

    cid = fig.canvas.mpl_connect("close_event", _on_close)

    def stop():
        try:
            import matplotlib.pyplot as _plt
            try:
                fig.canvas.mpl_disconnect(cid)
            except Exception:
                pass
            _plt.close(fig)
        finally:
            _on_close()

    plt.show(block=blocking)
    if not blocking:
        plt.pause(0.05)

    return stop

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