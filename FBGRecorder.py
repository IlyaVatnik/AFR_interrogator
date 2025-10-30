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

__version__='1.0'
__date__='27.10.2025'



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
    record_channel: Optional[int] = None
    # Расширенная фильтрация: список каналов и FBG (0-based). Если None — писать всё.
    record_channels: Optional[List[int]] = None
    record_fbg_map: Optional[List[List[int]]] = None
    
    # Новый параметр: записывать только каждый n-ый кадр (после прогрева). 1 = писать каждый.
    write_every_n: int = 1


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
                fbg_map: Optional[List[List[int]]] = None) -> Dict[str, Any]:
    """
    Строит заголовок. Если задан channel_map/fbg_map — пишется с version=5
    и полями:
      - original_channels, original_fbg_per_ch
      - channel_map: List[int] (0-based индексы каналов)
      - fbg_map: List[List[int]] (0-based индексы решёток по каждому каналу)
    Если channel_map/fbg_map не заданы — поведение как раньше (version=4).
    """
    hdr: Dict[str, Any] = {
        "channels": int(getattr(it, "channels", 0)),
        "fbg_per_ch": int(getattr(it, "fbg_per_ch", 0)),
        "version": 4,
        "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_ch][fbg])",
    }
    if channel_map is not None or fbg_map is not None:
        cm = list(map(int, channel_map or list(range(int(getattr(it, "channels", 0))))))
        fm = []
        orig_fbg = int(getattr(it, "fbg_per_ch", 0))
        if fbg_map is None:
            # по умолчанию: для каждого выбранного канала — все FBG
            fm = [list(range(orig_fbg)) for _ in cm]
        else:
            fm = [list(map(int, arr)) for arr in fbg_map]

        hdr.update({
            "version": 5,
            "original_channels": int(getattr(it, "channels", 0)),
            "original_fbg_per_ch": orig_fbg,
            "channel_map": cm,
            "fbg_map": fm,
            "channels": len(cm),
            # fbg_per_ch оставляем как «исходное» — для обратной совместимости
            "fbg_per_ch": orig_fbg,
            "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_selected_ch][n_selected_fbg_per_ch])",
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

                header = make_header(self.it, channel_map=ch_map, fbg_map=fbg_map)
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
                   write_every_n: int = 1   
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
    
    
    
    configure_headless_matplotlib()
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
        record_channels=rec_channels_zb,
        record_fbg_map=rec_fbg_map_zb,
        write_every_n=int(write_every_n)
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
    """
    Возвращает:
      - times: np.ndarray [n_samples], секунд от первого кадра
      - channel_FBGs: List[np.ndarray], длиной n_selected_ch;
        каждый элемент — np.ndarray формы [n_selected_fbg(ch), n_samples].

    Поддерживает файлы:
      - version 4: равное кол-во FBG на канал (header["fbg_per_ch"])
      - version 5: выбор каналов/FBG (header["channel_map"], header["fbg_map"])
    """
    import numpy as np
    with open(filepath, "rb") as f:
        header = pickle.load(f)
        version = int(header.get("version", 4))

        if version >= 5 and ("channel_map" in header) and ("fbg_map" in header):
            channel_map = list(map(int, header["channel_map"]))
            fbg_map = [list(map(int, row)) for row in header["fbg_map"]]
            n_ch = len(channel_map)
            fbg_counts = [len(row) for row in fbg_map]
        else:
            channel_map = None
            fbg_map = None
            n_ch = int(header["channels"])
            fbg_per_ch = int(header["fbg_per_ch"])
            fbg_counts = [fbg_per_ch] * n_ch

        t_perf: List[float] = []
        # acc[ch][i] -> list[float]
        acc: List[List[List[float]]] = [[[] for _ in range(fbg_counts[ch])] for ch in range(n_ch)]

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

                # wl должен быть длиной n_ch
                if not isinstance(wl, (list, tuple)):
                    wl = []
                if len(wl) != n_ch:
                    wl = (list(wl) + [[]] * n_ch)[:n_ch]

                for ch in range(n_ch):
                    row = wl[ch]
                    # нормализуем до нужного количества FBG для этого канала
                    need = fbg_counts[ch]
                    cur = []
                    if isinstance(row, (list, tuple)):
                        cur = [float(x) for x in row[:need]]
                    # добить NaN при нехватке
                    if len(cur) < need:
                        cur = cur + [float("nan")] * (need - len(cur))
                    for i in range(need):
                        acc[ch][i].append(cur[i])

    
    t_perf_arr = np.asarray(t_perf, dtype=float)
    if t_perf_arr.size == 0:
        return t_perf_arr, []
    t0 = t_perf_arr[0]
    times = t_perf_arr - t0
    channel_FBGs = [np.asarray(acc[ch], dtype=float) for ch in range(len(acc))]
    return times, channel_FBGs

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
                    # НОВОЕ: выборка для записи (1-based)
                    channels: Optional[List[int]] = None,
                    FBGs: Optional[List[List[int]]] = None,
                    # НОВОЕ: писать каждый n-ый кадр
                    write_every_n: int = 1,
                    # live-плот
                    plot_channel: int = 1,  # ВНИМАНИЕ: для пользователя 1-based (как в docstring live_plot)
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

    FSYNC_EVERY_BATCHES = 20
    BATCH_SIZE = 1000
    WARMUP_SEC = 1.0
    DROP_DURING_WARMUP = True
    START_DELAY_SEC = 0.3
    DISABLE_GC_DURING_RECORD = True
    
    # Подготовим карты выбора (1-based -> 0-based)
    ch_map_0 = None
    fbg_map_0 = None
    if channels is not None:
        ch_map_0 = [int(c) - 1 for c in channels]
        if FBGs is not None:
            if len(FBGs) != len(ch_map_0):
                raise ValueError("Длина FBGs должна совпадать с длиной channels")
            fbg_map_0 = [[int(i) - 1 for i in arr] for arr in FBGs]

    # Небольшая задержка для старта потока устройства (внутренняя, фиксированная)
    if START_DELAY_SEC > 0:
        time.sleep(START_DELAY_SEC)

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

        # частота отбора: каждый n‑ый
        write_every = max(1, int(write_every_n))
        taken_ctr = 0

        try:
            with open(filepath, "wb") as f:
                # Заголовок: добавляем карты, если заданы
                header = make_header(it, channel_map=ch_map_0, fbg_map=fbg_map_0)
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

                    # Отбор каждого n-го
                    taken_ctr += 1
                    if (taken_ctr % write_every) != 0:
                        continue

                    # Фильтрация каналов/FBG для записи
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
                    rec = (float(t_perf), float(ts_unix), int(pkt_ctr), wl_rows)

                    batch.append(rec)
                    stats["wr_frames"] += 1
                    wr_count_since += 1

                    if writing_active and (len(batch) >= BATCH_SIZE):
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
            if DISABLE_GC_DURING_RECORD and gc_was_enabled and not gc.isenabled():
                gc.enable()

    wr_thr = threading.Thread(target=writer_thread, name="FBG-Writer", daemon=True)
    wr_thr.start()

    # Live‑плот — без изменений производительности; канал в live_plot — 1-based (как и было)
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
        try:
            stop_plot()
        except Exception:
            pass
        stop_event.set()
        try:
            wr_thr.join(timeout=2.0)
        except Exception:
            pass
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
    нумерация канала - с первого
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

    import matplotlib.pyplot as plt


    # Валидация индексов
    
    n_ch = int(getattr(it, "channels", 0) or 0)
    fbg_per_ch = int(getattr(it, "fbg_per_ch", 0) or 0)
    if n_ch > 0 and not (1 <= channel < n_ch+1):
        raise ValueError(f"Некорректный channel={channel}, допустимо 1..{n_ch}")
    channel=channel-1 # весь дальнейшей код в этой функции работает с нумерацией каналов 0..n-1
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