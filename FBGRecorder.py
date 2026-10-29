# -*- coding: utf-8 -*-
"""
FBGrecorder.py — безопасная безголовая запись потока FBG в файл и live-плот
(интерактивные окна PyQt5/pyqtgraph, см. live_plot_qt.py).

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
Live-плот: live_plot_wavelengths(it, channel, fbg_indices, ...) — запускайте из главного потока GUI
(нужен созданный QApplication).
"""

__version__ = '3.0.0'
__date__ = '2026.10.27'

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Any, Callable, Dict, List, Optional, Tuple, Iterable
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
    FBGs: Optional[List[List[int]]] = None,      # 1-based

    record_channel: Optional[int] = None
    # Расширенная фильтрация: список каналов и FBG (0-based). Если None — писать всё.
    record_channels: Optional[List[int]] = None
    record_fbg_map: Optional[List[List[int]]] = None

    # Новый параметр: записывать только каждый n-ый кадр (после прогрева). 1 = писать каждый.
    write_every_n: int = 1
    other_params: Optional[Dict] = None  # любые другие параметры, которые надо сохранить в файл


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
    version=5: хранит 0-based channel_map/fbg_map
    version=6: дополнительно хранит 1-based channel_list/FBGs_list

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
        cm = list(map(int, channel_map or list(range(orig_channels))))

        if fbg_map is None:
            fm = [list(range(orig_fbg)) for _ in cm]
        else:
            fm = [list(map(int, arr)) for arr in fbg_map]

        channel_list = [c + 1 for c in cm]
        FBGs_list = [[i + 1 for i in row] for row in fm]

        hdr.update({
            "version": 6,
            "original_channels": orig_channels,
            "original_fbg_per_ch": orig_fbg,

            "channel_map": cm,
            "fbg_map": fm,

            "channel_list": channel_list,
            "FBGs_list": FBGs_list,

            "channels": len(cm),
            "fbg_per_ch": orig_fbg,
            "format": "(ts_perf, ts_unix, pkt_ctr, wl[n_selected_ch][n_selected_fbg_per_ch])",
            "other_params": other_params,
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
    """Переводит Matplotlib в headless-режим, закрывает все окна."""
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
# Fanout: один читатель pop_freq_frame() на приложение/рекордер
# ==========================

class FrameFanout:
    """
    Один поток читает it.pop_freq_frame() и рассылает кадры всем подписчикам (очередям).

    Элемент кадра (полный):
        (ts_perf: float, ts_unix: float, pkt_ctr: int, wl_full: List[List[float]])

    Фильтр скачков:
      - порог берётся из it.params.max_wl_jump_nm (если есть)
      - если abs(delta) > thr хотя бы для одной решётки -> кадр отбрасывается
    """
    def __init__(self, it: Any, idle_sleep: float = 0.0002):
        self.it = it
        self.idle_sleep = float(idle_sleep)

        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None
        self._queues: List["Queue[Tuple[float, float, int, List[List[float]]]]"] = []
        self._lock = threading.Lock()

        self._prev_wl: Optional[List[List[float]]] = None
        self._prev_np: Optional[np.ndarray] = None   # то же, но ndarray (быстрый путь)
        self.threshold = getattr(getattr(self.it, "params", None), "max_wl_jump_nm", None)
        self.jump_count = 0

    def add_consumer_queue(self, q: "Queue[Tuple[float, float, int, List[List[float]]]]"):
        with self._lock:
            self._queues.append(q)

    def remove_consumer_queue(self, q):
        with self._lock:
            self._queues = [qq for qq in self._queues if qq is not q]

    def _is_jump_frame(self, wl_cur: Any) -> bool:
        
        if self.threshold is None or self.threshold <= 0:
            return False

        prev = self._prev_wl
        if prev is None:
            return False

        if not isinstance(wl_cur, (list, tuple)):
            return False

        n_ch = min(len(prev), len(wl_cur))
        for ch in range(n_ch):
            row_prev = prev[ch]
            row_cur = wl_cur[ch]
            if not isinstance(row_prev, (list, tuple)) or not isinstance(row_cur, (list, tuple)):
                continue
            n_fbg = min(len(row_prev), len(row_cur))
            for i in range(n_fbg):
                try:
                    a = float(row_prev[i])
                    b = float(row_cur[i])
                except Exception:
                    continue
                if a != a or b != b:  # NaN
                    continue
                if abs(b - a) > self.threshold:
                    return True
        return False

    def start(self):
        if self._thr and self._thr.is_alive():
            return

        def _loop():
            while not self._stop.is_set():
                try:
                    batch = self.it.pop_freq_frames_batch(max_batch=512)
                    if not batch:
                        time.sleep(self.idle_sleep)
                        continue

                    with self._lock:
                        qs = list(self._queues)

                    for fr in batch:
                        if not isinstance(fr, dict):
                            continue

                        wl = fr.get("wavelength_nm")
                        if wl is None:
                            continue

                        wl_np = fr.get("wl_np")
                        if wl_np is not None:
                            # Быстрый путь: векторная проверка скачков без Python-циклов.
                            # NaN в сравнениях дают False, т.е. пропускаются, как и раньше.
                            prev_np = self._prev_np
                            thr = self.threshold
                            if (thr is not None and thr > 0 and prev_np is not None
                                    and prev_np.shape == wl_np.shape
                                    and bool((np.abs(wl_np - prev_np) > thr).any())):
                                self.jump_count += 1
                                continue
                            self._prev_np = wl_np      # кадры не мутируются -> копия не нужна
                        else:
                            if self._is_jump_frame(wl):
                                self.jump_count += 1
                                continue
                            try:
                                self._prev_wl = [list(row) for row in wl]
                            except Exception:
                                self._prev_wl = None

                        ts_perf = float(fr.get("t_perf", time.perf_counter()))
                        ts_unix = float(fr.get("timestamp", time.time()))
                        pkt_ctr = int(fr.get("pkt_counter_be32", -1))

                        item = (ts_perf, ts_unix, pkt_ctr, wl)

                        for q in qs:
                            try:
                                q.put_nowait(item)
                            except Exception:
                                try:
                                    _ = q.get_nowait()
                                    q.put_nowait(item)
                                except Exception:
                                    pass
                except Exception as e:
                    print(f"FBG-Fanout _loop error: {e!r}")
                    time.sleep(0.01)

        self._thr = threading.Thread(target=_loop, name="FBG-Fanout", daemon=True)
        self._thr.start()

    def stop(self, timeout: float = 1.0):
        self._stop.set()
        try:
            if self._thr:
                self._thr.join(timeout=timeout)
        except Exception:
            pass
        if self.jump_count > 0:
            print(f"FrameFanout: {self.jump_count} jump(s) detected and dropped")


# ==========================
# Основной класс рекордера
# ==========================

class FBGRecorder:
    """
    Безголовый двухпоточный рекордер.

    NEW (важно):
      - рекордер создаёт свой FrameFanout
      - FrameFanout — единственный читатель it.pop_freq_frame()
      - writer читает очередь с полными кадрами (ts_perf, ts_unix, pkt_ctr, wl_full)
        и применяет фильтрацию каналов/FBG перед записью
    """
    def __init__(self, it: Any, cfg: RecorderConfig):
        self.it = it
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._q: Queue = Queue(maxsize=cfg.queue_max)
        self._rx_thread: Optional[threading.Thread] = None   # не используется, оставлен для совместимости
        self._wr_thread: Optional[threading.Thread] = None
        self._stats = RecorderStats()
        self._gc_was_enabled: bool = False

        # NEW
        self._fan: Optional[FrameFanout] = None
        self._fan_q: Optional[Queue] = None

    def start(self) -> None:
        if self._rx_thread or self._wr_thread:
            raise RuntimeError("Recorder уже запущен")

        self._stats.started_at = time.perf_counter()

        if self.cfg.start_delay_sec > 0:
            time.sleep(self.cfg.start_delay_sec)

        import gc
        self._gc_was_enabled = gc.isenabled()
        if self.cfg.disable_gc_during_record and self._gc_was_enabled:
            gc.disable()

        # NEW: единственный читатель pop_freq_frame
        self._fan = FrameFanout(self.it, idle_sleep=self.cfg.idle_sleep_empty_ring)
        self._fan_q = self._q
        self._fan.add_consumer_queue(self._fan_q)
        self._fan.start()

        self._wr_thread = threading.Thread(target=self._writer_loop, name="FBGWriter", daemon=True)
        self._wr_thread.start()

        self._rx_thread = None  # больше не используем

    def stop(self) -> None:
        self._stop_event.set()
        self._done_event.wait(timeout=max(1.0, self.cfg.duration_sec + 2.0))

        # остановить fanout
        try:
            if self._fan is not None and self._fan_q is not None:
                self._fan.remove_consumer_queue(self._fan_q)
        except Exception:
            pass
        try:
            if self._fan is not None:
                self._fan.stop(timeout=1.0)
        except Exception:
            pass
        self._fan = None
        self._fan_q = None

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        return self._done_event.wait(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return not self._done_event.is_set()

    def stats(self) -> Dict[str, Any]:
        return self._stats.snapshot()

    def _writer_loop(self) -> None:
        """Запись length-prefixed батчей в файл с фазой прогрева/стабилизации."""
        import gc

        t_start = self._stats.started_at
        t_end = t_start + float(self.cfg.duration_sec)
        blocks_written = 0
        wr_count_since = 0
        last_wr = time.perf_counter()

        writing_active = False
        warmup_deadline = t_start + max(0.0, self.cfg.warmup_sec)
        write_every_n = max(1, int(getattr(self.cfg, "write_every_n", 1)))
        taken_ctr = 0

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
                        fbg_map = [list(map(int, arr)) for arr in self.cfg.record_fbg_map]
                elif self.cfg.record_channel is not None:
                    ch_map = [int(self.cfg.record_channel)]

                header = make_header(self.it, channel_map=ch_map, fbg_map=fbg_map, other_params=self.cfg.other_params)
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

                # rx fps оценим по приходу элементов в очередь (с небольшим лагом)
                rx_count_since = 0
                last_rx_stat = time.perf_counter()

                def _row_to_list(row):
                    if not isinstance(row, (list, tuple)):
                        try:
                            row = list(row)
                        except Exception:
                            row = []
                    return [float(x) for x in row]

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

                    # rec ожидается как (ts_perf, ts_unix, pkt_ctr, wl_full)
                    try:
                        ts_perf, ts_unix, pkt_ctr, wl_full = rec
                    except Exception:
                        # если в очередь внезапно попал старый формат (t, wl)
                        try:
                            ts_perf, wl_full = rec
                            ts_unix = time.time()
                            pkt_ctr = -1
                        except Exception:
                            continue

                    self._stats.rx_frames += 1
                    rx_count_since += 1

                    # rx fps
                    if (now - last_rx_stat) >= self.cfg.log_stats_interval:
                        self._stats.rx_fps = rx_count_since / (now - last_rx_stat)
                        rx_count_since = 0
                        last_rx_stat = now

                    # В прогреве можно дропать, чтобы не копить задержку
                    if not writing_active and self.cfg.drop_during_warmup:
                        continue

                    if writing_active:
                        taken_ctr += 1
                        if (taken_ctr % write_every_n) != 0:
                            continue

                    # --- фильтрация каналов/FBG ---
                    if self.cfg.record_channels is not None or self.cfg.record_channel is not None:
                        if self.cfg.record_channels is not None:
                            ch_sel = [int(x) for x in self.cfg.record_channels]
                        else:
                            ch_sel = [int(self.cfg.record_channel)]  # type: ignore[arg-type]

                        wl_filtered: List[List[float]] = []
                        for idx, ch0 in enumerate(ch_sel):
                            if isinstance(wl_full, (list, tuple)) and 0 <= ch0 < len(wl_full):
                                row_full = _row_to_list(wl_full[ch0])
                            else:
                                row_full = []

                            if self.cfg.record_fbg_map is not None:
                                fbg_map_for_ch = self.cfg.record_fbg_map[idx] if idx < len(self.cfg.record_fbg_map) else []
                                row_sel: List[float] = []
                                for fbg in fbg_map_for_ch:
                                    fbg_i = int(fbg)
                                    if 0 <= fbg_i < len(row_full):
                                        row_sel.append(row_full[fbg_i])
                                    else:
                                        row_sel.append(float("nan"))
                                wl_filtered.append(row_sel)
                            else:
                                wl_filtered.append(row_full)
                    else:
                        wl_filtered = [_row_to_list(r) for r in (wl_full if isinstance(wl_full, (list, tuple)) else [])]

                    batch.append((float(ts_perf), float(ts_unix), int(pkt_ctr), wl_filtered))
                    self._stats.wr_frames += 1
                    wr_count_since += 1

                    if writing_active and (len(batch) >= self.cfg.batch_size):
                        flush_batch()

                    if (now - last_wr) >= self.cfg.log_stats_interval:
                        self._stats.wr_fps = wr_count_since / (now - last_wr)
                        wr_count_since = 0
                        last_wr = now

                    # оценка длины ring (без лока, не критично)
                    try:
                        self._stats.ring_len = len(self.it._ring)  # type: ignore[attr-defined]
                    except Exception:
                        pass

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
                   FBGs: Optional[List[List[int]]] = None,      # 1-based
                   write_every_n: int = 1,
                   other_params: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Если заданы channels/FBGs — записывается только это подмножество.
    channels — список каналов (1-based). FBGs — список списков FBG (1-based) на каждый канал.
    Если channels задан, а FBGs — нет: будут записаны все FBG выбранных каналов.
    """

    batch_size = 1000
    queue_max = 50000
    fsync_every_batches = 20
    idle_sleep_empty_ring = 0.0002
    start_delay_sec = 0.5
    warmup_sec = 1.5
    drop_during_warmup = True
    min_rate_hz = 1500.0
    rate_window_sec = 0.8
    disable_gc_during_record = True

    import gc as _gc
    _gc.collect()

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
                           channels: Optional[List[int]] = None) -> None:

    dtype = np.float32
    n_ch = len(channels)
    max_acq_rate = 2000  # Hz
    max_cols = int(duration_sec * max_acq_rate / 1 / n_ch)
    print(max_cols)
    data_path = Path(filepath)
    meta_path = data_path.with_suffix(data_path.suffix + '.meta')

    waves = it.get_waves()
    n_rows = len(waves)

    mm = np.memmap(data_path, dtype=dtype, mode="w+", shape=(n_ch, n_rows, max_cols))
    time_start = time.time()
    time_current = time_start
    times_array = []
    jj = 0
    while time_current - time_start < duration_sec:
        for ii, ch in enumerate(channels):
            spectrum = it.get_single_spectrum(ch)
            mm[ii, :, jj] = spectrum
        times_array.append(time_current - time_start)
        time_current = time.time()
        jj += 1

    times_array = np.array(times_array)
    mm.flush()
    dictionary = {}
    dictionary['n_ch'] = n_ch
    dictionary['n_rows'] = n_rows
    dictionary['n_cols'] = jj
    dictionary['max_cols'] = max_cols
    dictionary['times'] = times_array
    dictionary['waves'] = waves
    dictionary['dtype'] = dtype
    dictionary['other_params'] = other_params
    with open(meta_path, 'wb') as f:
        pickle.dump(dictionary, f)


def read_spectra_from_file(filepath: str, channel: int):
    data_path = Path(filepath)
    meta_path = data_path.with_suffix(data_path.suffix + '.meta')
    with open(meta_path, 'rb') as f:
        meta = pickle.load(f)
    n_ch = int(meta["n_ch"])
    n_rows = int(meta["n_rows"])
    max_cols = int(meta["max_cols"])
    n_cols = int(meta["n_cols"])
    waves = meta['waves']
    times = meta['times']
    other_params = meta['other_params']
    dtype = (meta['dtype'])

    mm = np.memmap(data_path, dtype=dtype, mode="r", shape=(n_ch, n_rows, max_cols))
    spectra = mm[channel - 1, :, :n_cols]  # view без копии

    return times, waves, spectra, other_params


def read_fbg_stream_raw_lp(filepath: str, debug: bool = False):
    """
    ПАТЧ:
      - читает до последнего корректного блока
      - при повреждённом/оборванном хвосте возвращает уже накопленные данные
      - если header повреждён/не дочитан -> выбрасывает понятное исключение
    """
    import numpy as np
    import pickle
    import struct
    from typing import List, Dict

    MAX_BLOCK_LEN = 1 << 30  # 1GB

    def _dbg(*a):
        if debug:
            print("[read_fbg_stream_raw_lp]", *a)

    with open(filepath, "rb") as f:
        # --- header ---
        try:
            header = pickle.load(f)
        except Exception as e:
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
                break
            if len(len_buf) < 4:
                break

            try:
                (block_len,) = struct.unpack(">I", len_buf)
            except Exception:
                break

            if block_len <= 0 or block_len > MAX_BLOCK_LEN:
                break

            blob = f.read(block_len)
            if len(blob) < block_len:
                break

            try:
                batch = pickle.loads(blob)
            except Exception:
                break

            for rec in batch:
                if isinstance(rec, tuple) and len(rec) == 4:
                    ts_p, ts_u, pkt_ctr, wl = rec
                    _append_record(ts_p, wl)

    t_perf_arr = np.asarray(t_perf, dtype=float)
    if t_perf_arr.size == 0:
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


# ==========================
# Live‑plot в реальном времени
# ==========================

def start_live_plot_session(it: Any,
                            plot_channels: List[int],          # 1-based
                            plot_FBGs: List[List[int]],        # 1-based (как в GUI params)
                            rep_rate_hz: float = 2000.0,
                            window_sec: float = 20.0,
                            max_fps: int = 30,
                            ylim: Optional[Tuple[float, float]] = None,
                            title_prefix: str = "Live",
                            use_subplots: bool = True,
                            queue_maxsize: int = 10000,
                            max_frames_per_update: int = 1200,
                            max_plot_points: int = 3000,
                            autoscale_every: int = 30) -> Tuple[Callable[[], None], Dict[str, Any]]:
    """
    Запускает:
      - FrameFanout (единственный читатель it.pop_freq_frame)
      - по одной очереди на канал
      - по одному live_plot_wavelengths на канал

    Примечание: max_plot_points/autoscale_every управляют децимацией и частотой
    автозума (то же, что и в динамических измерениях).
    """
    from queue import Queue

    fan = FrameFanout(it, idle_sleep=0.0002)
    stops: List[Callable[[], None]] = []
    figs = []
    queues = []

    for ch1 in plot_channels:
        q = Queue(maxsize=int(queue_maxsize))
        fan.add_consumer_queue(q)
        queues.append(q)

    fan.start()

    for idx, ch1 in enumerate(plot_channels):
        fbg1_list = plot_FBGs[idx] if idx < len(plot_FBGs) else []
        fbg0 = [int(x) - 1 for x in fbg1_list]

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
            max_plot_points=int(max_plot_points),
            autoscale_every=int(autoscale_every),
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


def start_live_spectra_session(it: Any,
                               channels: List[int],
                               waves=None,
                               interval_ms: int = 80,
                               title_prefix: str = "Live spectra",
                               dot_color: str = "red",
                               markersize: float = 8.0,
                               on_error=None,
                               on_status=None):
    """
    Live-графики спектров отражения (по одному окну на канал).

    Начиная с v3.0.0 — интерактивные окна PyQt5/pyqtgraph, см. live_plot_qt.
    Вызывать из GUI-потока. Возвращает (stop_all, info).
    """
    try:
        from .live_plot_qt import start_live_spectra_session as _impl
    except ImportError:
        from live_plot_qt import start_live_spectra_session as _impl
    return _impl(it, channels, waves=waves, interval_ms=interval_ms,
                 title_prefix=title_prefix, dot_color=dot_color,
                 markersize=markersize, on_error=on_error, on_status=on_status)


def record_and_plot(it: Any,
                    filepath: str,
                    duration_sec: float,
                    channels: Optional[List[int]] = None,
                    FBGs: Optional[List[List[int]]] = None,
                    write_every_n: int = 1,
                    plot_channel: int = 1,
                    plot_fbg_indices: List[int] = (0, 1, 2),
                    plot_channels: Optional[List[int]] = None,          # 1-based
                    plot_FBGs: Optional[List[List[int]]] = None,        # 0-based indices per channel (for plotting)
                    use_subplots: bool = True,
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

    # fanout теперь кладёт (t_perf, ts_unix, pkt_ctr, wl_full)
    q_rec = Queue(maxsize=50000)

    fan = FrameFanout(it, idle_sleep=0.0002)
    fan.add_consumer_queue(q_rec)
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
                        item = q_rec.get(timeout=min(0.1, max(0.0, t_end - now)))
                    except Empty:
                        if writing_active:
                            flush_batch()
                        continue

                    if not writing_active and DROP_DURING_WARMUP:
                        continue

                    # совместимость: (t, wl) или (t, ts_unix, pkt_ctr, wl)
                    if isinstance(item, tuple) and len(item) == 2:
                        t_perf, wl_full = item
                        ts_unix = time.time()
                        pkt_ctr = -1
                    else:
                        t_perf, ts_unix, pkt_ctr, wl_full = item

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

    # По отдельной очереди на каждый график канала: иначе каналы делят одну очередь
    # и на графиках появляются пропуски.
    q_plots = []
    for _ in plot_channels:
        _q = Queue(maxsize=10000)
        fan.add_consumer_queue(_q)
        q_plots.append(_q)

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
            source_queue=q_plots[k],
            use_subplots=use_subplots,
            expected_rate_hz=float(getattr(getattr(it, 'params', None), 'rep_rate', 2000.0) or 2000.0)
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

    try:
        # QTimer.singleShot: сработает в GUI-потоке (record_and_plot вызывается из него)
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(int(float(duration_sec) * 1000), stop_all)
    except Exception:
        threading.Timer(duration_sec, stop_all).start()

    return stop_all, stats


# Настройки отрисовки live-плота (можно менять до запуска измерения).
# Устарело с v3.0.0 (децимацию по пикселям делает pyqtgraph); оставлено для совместимости.
LIVE_PLOT_POINTS_PER_PX = 1.0
# Сглаживание линий: False заметно ускоряет рендер (линии чуть «ступенчатее»).
LIVE_PLOT_ANTIALIASED = True


def _decimate_envelope_multi(x, Y, max_points: int):
    """
    Векторное прореживание для отрисовки с сохранением огибающей (min/max по бинам).

    x : (N,)    — общая ось (время)
    Y : (S, N)  — S рядов (по одному на линию)

    Возвращает (X_out, Y_out), каждая формы (S, M), где M <= max_points. В каждом
    бине берутся точки min и max ряда, поэтому узкие FBG-дипы не теряются; точки
    внутри бина упорядочены по времени. Если N <= max_points, возвращается
    исходное (x повторяется для всех рядов).

    Вся работа — несколько numpy-вызовов на ВСЕ ряды сразу (раньше был питоновский
    цикл по бинам на каждую линию, что и тормозило отрисовку).
    """
    S, N = Y.shape
    if max_points <= 0 or N <= max_points or max_points < 4:
        return np.broadcast_to(x, (S, N)), Y

    nbins = max_points // 2
    k = -(-N // nbins)                  # ceil(N / nbins): размер бина
    Npad = k * nbins
    if Npad != N:
        # добиваем хвост повтором последней точки (на min/max не влияет:
        # argmin/argmax берут первое вхождение, т.е. реальную точку)
        Yp = np.concatenate([Y, np.repeat(Y[:, -1:], Npad - N, axis=1)], axis=1)
    else:
        Yp = Y

    seg = Yp.reshape(S, nbins, k)
    # NaN -> игнорируем при поиске экстремумов, но не выбрасываем целиком бин из NaN
    with np.errstate(invalid="ignore"):
        i_min = np.argmin(np.where(np.isnan(seg), np.inf, seg), axis=2)
        i_max = np.argmax(np.where(np.isnan(seg), -np.inf, seg), axis=2)
    base = (np.arange(nbins, dtype=np.int64) * k)[None, :]
    i_min = np.minimum(base + i_min, N - 1)
    i_max = np.minimum(base + i_max, N - 1)
    lo = np.minimum(i_min, i_max)
    hi = np.maximum(i_min, i_max)
    idx = np.stack([lo, hi], axis=2).reshape(S, 2 * nbins)      # неубывающие по времени
    return x[idx], np.take_along_axis(Y, idx, axis=1)


def _decimate_envelope(x, y, max_points: int):
    """
    Прореживает один ряд для отрисовки, сохраняя огибающую (в т.ч. узкие FBG-дипы).
    Обёртка над _decimate_envelope_multi (оставлена для обратной совместимости).
    """
    if x is None or y is None:
        return x, y
    y = np.asarray(y, dtype=float)
    if y.size <= max_points or max_points <= 0:
        return x, y
    X2, Y2 = _decimate_envelope_multi(np.asarray(x, dtype=float), y[None, :], max_points)
    return X2[0], Y2[0]


class _ScrollBuffer:
    """
    Кольцевой буфер времени + S рядов на numpy (замена deque + np.fromiter).

    Хранит данные в массиве удвоенной ёмкости: окно всегда доступно как непрерывный
    срез БЕЗ копирования, а добавление пачки кадров — один векторный вызов.
    Когда массив заполняется, последние `capacity` отсчётов сдвигаются в начало
    (амортизированная стоимость O(1) на отсчёт).
    """
    def __init__(self, n_series: int, capacity: int):
        self.cap = int(capacity)
        self.t = np.empty(2 * self.cap, dtype=np.float64)
        self.y = np.empty((int(n_series), 2 * self.cap), dtype=np.float64)
        self.n = 0

    def append(self, t_new: np.ndarray, y_new: np.ndarray) -> None:
        m = t_new.size
        if m == 0:
            return
        if m > self.cap:                       # пришло больше, чем влезает: берём хвост
            t_new = t_new[-self.cap:]
            y_new = y_new[:, -self.cap:]
            m = self.cap
        if self.n + m > 2 * self.cap:          # сдвиг: оставляем cap - m последних
            keep = min(self.n, self.cap - m)
            if keep > 0:
                self.t[:keep] = self.t[self.n - keep:self.n].copy()
                self.y[:, :keep] = self.y[:, self.n - keep:self.n].copy()
            self.n = keep
        self.t[self.n:self.n + m] = t_new
        self.y[:, self.n:self.n + m] = y_new
        self.n += m

    def window(self, t_cut: float):
        """Срез (t, Y) c t >= t_cut — views, без копирования."""
        t = self.t[:self.n]
        i0 = int(np.searchsorted(t, t_cut, side="left"))
        return t[i0:], self.y[:, i0:self.n]


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
                          max_frames_per_update: int = 50000,
                          autoscale_every: int = 10,
                          max_plot_points: int = 3000,
                          shift_provider=None,
                          relative_time: bool = True,
                          use_blit: bool = True,
                          geometry=None):
    """
    Live-график длин волн FBG от source_queue (элементы (t_perf, wl_full) или
    (t_perf, ts_unix, pkt_ctr, wl_full)); функция НЕ читает it.pop_freq_frame.

    Начиная с v3.0.0 — интерактивное окно PyQt5/pyqtgraph (зум, панорамирование,
    пауза, перекрестие, экспорт), см. live_plot_qt.live_plot_wavelengths.
    Вызывать из GUI-потока. Возвращает (stop, window), где window — QWidget.
    """
    try:
        from .live_plot_qt import live_plot_wavelengths as _impl
    except ImportError:
        from live_plot_qt import live_plot_wavelengths as _impl
    return _impl(it, channel, fbg_indices, window_sec=window_sec,
                 expected_rate_hz=expected_rate_hz, max_fps=max_fps, title=title,
                 ylim=ylim, blocking=blocking, source_queue=source_queue,
                 use_subplots=use_subplots, max_frames_per_update=max_frames_per_update,
                 autoscale_every=autoscale_every, max_plot_points=max_plot_points,
                 shift_provider=shift_provider, relative_time=relative_time,
                 use_blit=use_blit, geometry=geometry)



def safe_stop_interrogator(it: Any, join_timeout: float = 2.0) -> None:
    """Аккуратно останавливает и очищает интеррогатор."""
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
                         channels: Optional[List[int]] = None,
                         FBGs: Optional[List[List[int]]] = None,
                         batch_size: int = 1,
                         other_params: Optional[Dict] = None,
                         timeout_single: float = 0.2,
                         init_timeout_sec: float = 10.0,
                         append: bool = False) -> Dict[str, Any]:
    """
    Долгая запись редких измерений через it.get_single_FBG_measurement() в файл формата.
    (Этот режим не использует pop_freq_frame и pkt_ctr всегда -1.)
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
        raise RuntimeError("Неизвестны it.channels/it.fbg_per_ch")

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

    if ch_map_0 is None and not append:
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
            raise RuntimeError("Не удалось получить initial single measurement")

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
            raise RuntimeError("В initial measurement нет ни одного непустого канала/решётки")

    if append:
        if ch_map_0 is None:
            raise ValueError("append=True требует явного задания channels (и желательно FBGs)")
    else:
        header = make_header(it, channel_map=ch_map_0, fbg_map=fbg_map_0, other_params=other_params)
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
            print('{:.2f} s '.format(ts_perf - t0_perf))
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


def record_to_file_from_queue(it: Any,
                              q_rec,
                              filepath: str,
                              duration_sec: float,
                              channels: Optional[List[int]] = None,
                              FBGs: Optional[List[List[int]]] = None,
                              write_every_n: int = 1,
                              other_params: Optional[Dict] = None,
                              warmup_sec: float = 1.0,
                              drop_during_warmup: bool = True,
                              batch_size: int = 100,
                              fsync_every_batches: int = 10) -> Dict[str, Any]:
    """
    Пишет .fbgs из очереди кадров q_rec, которую наполняет FrameFanout.

    Элемент очереди:
      - (t_perf, wl_full)  [старый формат]
      - (t_perf, ts_unix, pkt_ctr, wl_full) [новый формат]
    """

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
                item = q_rec.get(timeout=min(0.1, max(0.0, t_end - now)))
 
            except Empty:
                if writing_active:
                    flush_batch()
                continue

            if not writing_active and drop_during_warmup:
                continue

            # совместимость: (t, wl) или (t, ts_unix, pkt_ctr, wl)
            if isinstance(item, tuple) and len(item) == 2:
                t_perf, wl_full = item
                ts_unix = time.time()
                pkt_ctr = -1
            else:
                t_perf, ts_unix, pkt_ctr, wl_full = item

            taken_ctr += 1
            if (taken_ctr % write_every) != 0:
                continue

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