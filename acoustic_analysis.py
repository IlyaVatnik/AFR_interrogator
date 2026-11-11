# -*- coding: utf-8 -*-
"""
acoustic_analysis.py — анализ акустических шумов, воспринимаемых решёткой (FBG).

Сигнал — колебания длины волны Брэгга λ(t). Здесь только численная часть
(numpy, без Qt и без scipy):

    Params_acoustic          — настройки анализатора (живут в «Measurement parameters»)
    make_uniform(...)        — приведение отсчётов к равномерной сетке, оценка fs
    welch_psd(...)           — односторонняя PSD методом Уэлча (pm²/Hz)
    spectrogram_psd(...)     — оконное Фурье (STFT) -> PSD(f, t), с блочным
                               усреднением столбцов для очень длинных записей
    psd_to_scale(...)        — PSD -> выбранная шкала (ASD / PSD dB / амплитуда)
    band_rms(...)            — СКО колебаний λ в полосе [f_min, f_max]

Единицы: длины волн во входных данных — нм, все спектры — в пм.

Нормировки (w — окно, S1 = Σw, S2 = Σw², fs — частота дискретизации):
    PSD  = 2 |X|² / (fs·S2)                 [pm²/Hz]  (односторонняя; DC и Найквист без ×2)
    ASD  = sqrt(PSD)                        [pm/√Hz]
    A    = 2 |X| / S1 = sqrt(2·PSD·ENBW)    [pm]      амплитуда синусоиды, ENBW = fs·S2/S1²
Проверка: для белого шума с СКО σ уровень PSD = σ²/(fs/2); синус с амплитудой
a даёт пик A ≈ a (для Flat top — точно, для Hann — с ошибкой «гребёнки» до −1.4 дБ).
"""

__version__ = '1.1.0'
__date__ = '2026.11.11'

from typing import Dict, Optional, Tuple

import numpy as np

NM_TO_PM = 1e3

WINDOW_FUNCTIONS = ('Hann', 'Hamming', 'Blackman', 'Flat top', 'Rectangular')
SPECTRUM_SCALES = ('ASD, pm/√Hz', 'PSD, dB re 1 pm²/Hz', 'Amplitude, pm')


class Params_acoustic:
    """Настройки акустического анализатора (имена = objectName виджетов без префикса)."""

    def __init__(self):
        self.acoustic_fft_window_s = 1.0            # длина окна Фурье T, с (Δf = 1/T)
        self.acoustic_n_averages = 8                # число усреднений Уэлча (перекрытие 50 %)
        self.acoustic_window_function = 'Hann'
        self.acoustic_spectrum_scale = SPECTRUM_SCALES[0]
        self.acoustic_f_min_hz = 1.0                # нижняя граница показа и полосы СКО
        self.acoustic_f_max_hz = 0.0                # 0 -> частота Найквиста
        self.acoustic_log_frequency_axis = True
        self.acoustic_update_interval_ms = 250      # период обновления live-графика
        self.acoustic_spectrogram_overlap_percent = 75.0


def settings_from_params(p) -> Dict:
    """Безопасно приводит Params_acoustic (значения могли прийти строками из GUI) к dict."""
    def _f(name, default, lo=None):
        try:
            v = float(getattr(p, name, default))
        except (TypeError, ValueError):
            v = float(default)
        if not np.isfinite(v):
            v = float(default)
        if lo is not None:
            v = max(lo, v)
        return v

    d = Params_acoustic()
    window = str(getattr(p, 'acoustic_window_function', d.acoustic_window_function))
    if window not in WINDOW_FUNCTIONS:
        window = d.acoustic_window_function
    scale = str(getattr(p, 'acoustic_spectrum_scale', d.acoustic_spectrum_scale))
    if scale not in SPECTRUM_SCALES:
        scale = d.acoustic_spectrum_scale
    return {
        'fft_window_s': _f('acoustic_fft_window_s', d.acoustic_fft_window_s, 1e-3),
        'n_averages': int(_f('acoustic_n_averages', d.acoustic_n_averages, 1)),
        'window': window,
        'scale': scale,
        'f_min_hz': _f('acoustic_f_min_hz', d.acoustic_f_min_hz, 0.0),
        'f_max_hz': _f('acoustic_f_max_hz', d.acoustic_f_max_hz, 0.0),
        'log_f': bool(getattr(p, 'acoustic_log_frequency_axis', d.acoustic_log_frequency_axis)),
        'update_ms': int(_f('acoustic_update_interval_ms', d.acoustic_update_interval_ms, 30)),
        'overlap': min(95.0, _f('acoustic_spectrogram_overlap_percent',
                                d.acoustic_spectrogram_overlap_percent, 0.0)) / 100.0,
    }


# ==========================
# Окна
# ==========================

def get_window(name: str, n: int) -> np.ndarray:
    """Периодическое (DFT-even) окно длины n."""
    n = int(n)
    if n <= 1:
        return np.ones(max(n, 1))
    k = np.arange(n) / n
    name = str(name)
    if name == 'Hann':
        return 0.5 - 0.5 * np.cos(2 * np.pi * k)
    if name == 'Hamming':
        return 0.54 - 0.46 * np.cos(2 * np.pi * k)
    if name == 'Blackman':
        return 0.42 - 0.5 * np.cos(2 * np.pi * k) + 0.08 * np.cos(4 * np.pi * k)
    if name == 'Flat top':
        a = (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368)
        return (a[0] - a[1] * np.cos(2 * np.pi * k) + a[2] * np.cos(4 * np.pi * k)
                - a[3] * np.cos(6 * np.pi * k) + a[4] * np.cos(8 * np.pi * k))
    return np.ones(n)


def enbw_hz(window: np.ndarray, fs: float) -> float:
    w = np.asarray(window, dtype=float)
    return float(fs * np.sum(w ** 2) / np.sum(w) ** 2)


# ==========================
# Подготовка данных
# ==========================

def fill_nans(y: np.ndarray) -> Tuple[np.ndarray, float]:
    """Линейно заполняет NaN по строкам. Возвращает (y, доля NaN)."""
    y = np.array(y, dtype=float, copy=True)
    one_d = (y.ndim == 1)
    if one_d:
        y = y[None, :]
    bad_frac = 0.0
    n = y.shape[1]
    x = np.arange(n)
    for r in range(y.shape[0]):
        bad = ~np.isfinite(y[r])
        nb = int(bad.sum())
        if nb == 0:
            continue
        bad_frac = max(bad_frac, nb / max(1, n))
        if nb == n:
            y[r] = 0.0
            continue
        y[r, bad] = np.interp(x[bad], x[~bad], y[r, ~bad])
    return (y[0] if one_d else y), bad_frac


def _counter_index(ctr: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """
    Индексы отсчётов по аппаратному счётчику пакетов, если он выглядит достоверным
    (строго возрастает с постоянным шагом, редкие пропуски кратны шагу).
    Шаг может быть > 1 (запись каждого n-го кадра). Иначе None.
    """
    if ctr is None:
        return None
    c = np.asarray(ctr, dtype=np.float64)
    if c.size < 16 or not np.all(np.isfinite(c)) or np.any(c < 0):
        return None
    d = np.diff(c)
    if np.any(d <= 0):
        return None
    step = float(np.median(d))
    if step < 1 or step != np.round(step):
        return None
    k = d / step
    if np.mean(d == step) < 0.9 or np.any(k != np.round(k)):
        return None
    idx = np.concatenate([[0.0], np.cumsum(k)]).astype(np.int64)
    if idx[-1] > 2 * c.size + 1000:        # слишком много «пропусков» — счётчику не верим
        return None
    return idx


def make_uniform(t: np.ndarray, Y: np.ndarray, ctr: Optional[np.ndarray] = None,
                 fs_hint: Optional[float] = None) -> Tuple[np.ndarray, float, Dict]:
    """
    Приводит ряды Y (S, N) к равномерной сетке.

    Время приёма t_perf содержит джиттер ОС/сети, поэтому сами отсчёты считаются
    равноотстоящими (как они и снимались интеррогатором), а t используется только
    для оценки средней частоты fs по всему интервалу. Если счётчик пакетов
    достоверен — потерянные кадры восстанавливаются линейной интерполяцией.

    Возвращает (Y_uniform, fs, info).
    """
    t = np.asarray(t, dtype=float)
    Y = np.asarray(Y, dtype=float)
    one_d = (Y.ndim == 1)
    if one_d:
        Y = Y[None, :]
    n = Y.shape[1]
    info = {'missing': 0, 'counter_used': False}

    idx = _counter_index(ctr)
    if idx is not None and idx.size == n:
        n_u = int(idx[-1]) + 1
        missing = n_u - n
        if missing > 0:
            grid = np.arange(n_u)
            Yu = np.empty((Y.shape[0], n_u))
            for r in range(Y.shape[0]):
                Yu[r] = np.interp(grid, idx, Y[r])
            Y = Yu
        info['missing'] = int(missing)
        info['counter_used'] = True
        n_eff = Y.shape[1]
    else:
        n_eff = n

    span = float(t[-1] - t[0]) if t.size > 1 else 0.0
    if span > 0 and n_eff > 1:
        fs = (n_eff - 1) / span
    elif fs_hint:
        fs = float(fs_hint)
    else:
        fs = 1.0
    return (Y[0] if one_d else Y), float(fs), info


# ==========================
# Спектры
# ==========================

def _detrend_linear(seg: np.ndarray) -> np.ndarray:
    """Удаляет среднее и линейный тренд вдоль последней оси (векторно)."""
    n = seg.shape[-1]
    x = np.arange(n, dtype=float) - (n - 1) / 2.0
    sxx = float(np.sum(x * x)) or 1.0
    mean = seg.mean(axis=-1, keepdims=True)
    slope = np.sum((seg - mean) * x, axis=-1, keepdims=True) / sxx
    return seg - mean - slope * x


def _psd_norm(nperseg: int) -> np.ndarray:
    nf = nperseg // 2 + 1
    k = np.full(nf, 2.0)
    k[0] = 1.0
    if nperseg % 2 == 0:
        k[-1] = 1.0
    return k


def welch_psd(y: np.ndarray, fs: float, nperseg: int, n_averages: Optional[int] = None,
              window: str = 'Hann', overlap: float = 0.5) -> Tuple[np.ndarray, np.ndarray, int]:
    """
    PSD методом Уэлча по ПОСЛЕДНИМ сегментам ряда.

    y : (N,) или (S, N) — в любых единицах (результат в единицах²/Hz)
    n_averages: максимум сегментов (берутся самые свежие); None — все.
    Возвращает (f, psd (S, F) или (F,), число усреднённых сегментов).
    """
    y = np.asarray(y, dtype=float)
    one_d = (y.ndim == 1)
    if one_d:
        y = y[None, :]
    S, N = y.shape
    nperseg = int(min(max(8, nperseg), N))
    hop = max(1, int(round(nperseg * (1.0 - overlap))))
    n_seg = 1 + (N - nperseg) // hop
    if n_averages is not None:
        n_seg = min(n_seg, int(max(1, n_averages)))
    start = N - nperseg - (n_seg - 1) * hop     # самые свежие сегменты
    w = get_window(window, nperseg)
    s2 = float(np.sum(w * w))
    idx = start + hop * np.arange(n_seg)[:, None] + np.arange(nperseg)[None, :]   # (M, n)
    seg = y[:, idx]                                                               # (S, M, n)
    seg = _detrend_linear(seg) * w
    X = np.fft.rfft(seg, axis=-1)
    psd = (np.abs(X) ** 2).mean(axis=1) / (fs * s2) * _psd_norm(nperseg)[None, :]
    f = np.fft.rfftfreq(nperseg, d=1.0 / fs)
    return f, (psd[0] if one_d else psd), int(n_seg)


def spectrogram_psd(y: np.ndarray, fs: float, nperseg: int, overlap: float = 0.75,
                    window: str = 'Hann', max_columns: int = 3000,
                    chunk_segments: int = 512) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """
    Спектрограмма (оконное Фурье) одного ряда y (N,).

    Если сегментов больше max_columns, соседние столбцы усредняются блоками
    (используются ВСЕ данные, каждый столбец — Уэлч по нескольким сегментам).

    Возвращает (t_centers [s, от начала ряда], f, PSD (F, M), сегментов на столбец).
    """
    y = np.asarray(y, dtype=float)
    N = y.size
    nperseg = int(min(max(8, nperseg), N))
    hop = max(1, int(round(nperseg * (1.0 - overlap))))
    n_seg = 1 + (N - nperseg) // hop
    group = max(1, int(np.ceil(n_seg / max(1, int(max_columns)))))
    n_col = int(np.ceil(n_seg / group))

    w = get_window(window, nperseg)
    s2 = float(np.sum(w * w))
    norm = _psd_norm(nperseg) / (fs * s2)
    nf = nperseg // 2 + 1

    sums = np.zeros((n_col, nf))
    counts = np.zeros(n_col)
    from numpy.lib.stride_tricks import sliding_window_view
    view = sliding_window_view(y, nperseg)[::hop]           # (n_seg, nperseg), без копии
    for s0 in range(0, n_seg, int(chunk_segments)):
        s1 = min(n_seg, s0 + int(chunk_segments))
        seg = _detrend_linear(np.array(view[s0:s1])) * w
        P = np.abs(np.fft.rfft(seg, axis=-1)) ** 2
        cols = np.arange(s0, s1) // group
        np.add.at(sums, cols, P)
        np.add.at(counts, cols, 1.0)
    psd = (sums / np.maximum(counts, 1.0)[:, None] * norm[None, :]).T   # (F, M)

    seg_starts = hop * np.arange(n_seg)
    centers = (seg_starts + nperseg / 2.0) / fs
    t_col = np.bincount(np.arange(n_seg) // group, weights=centers) / np.bincount(np.arange(n_seg) // group)
    f = np.fft.rfftfreq(nperseg, d=1.0 / fs)
    return t_col, f, psd, group


def psd_to_scale(psd: np.ndarray, scale: str, window: str, nperseg: int, fs: float) -> np.ndarray:
    """PSD [pm²/Hz] -> выбранная шкала."""
    psd = np.asarray(psd, dtype=float)
    if scale.startswith('PSD'):
        with np.errstate(divide='ignore'):
            return 10.0 * np.log10(np.maximum(psd, 1e-30))
    if scale.startswith('Amplitude'):
        return np.sqrt(2.0 * psd * enbw_hz(get_window(window, nperseg), fs))
    return np.sqrt(psd)


def scale_units(scale: str) -> Tuple[str, str, bool]:
    """(подпись оси, короткое имя величины, логарифмическая ли ось Y)."""
    if scale.startswith('PSD'):
        return 'PSD, dB re 1 pm²/Hz', 'PSD', False
    if scale.startswith('Amplitude'):
        return 'Amplitude, pm', 'A', True
    return 'ASD, pm/√Hz', 'ASD', True


def band_rms(f: np.ndarray, psd: np.ndarray, f_min: float, f_max: float) -> np.ndarray:
    """СКО в полосе [f_min, f_max] (DC исключён), в единицах sqrt(PSD·Hz)."""
    f = np.asarray(f, dtype=float)
    psd = np.asarray(psd, dtype=float)
    if f.size < 2:
        return np.zeros(psd.shape[:-1]) if psd.ndim > 1 else 0.0
    df = f[1] - f[0]
    hi = f_max if (f_max and f_max > 0) else f[-1]
    m = (f > 0) & (f >= f_min) & (f <= hi)
    return np.sqrt(np.sum(psd[..., m], axis=-1) * df)


def freq_limits(f_min: float, f_max: float, fs: float, df: float, log_f: bool) -> Tuple[float, float]:
    """Диапазон частот для показа."""
    nyq = fs / 2.0
    hi = f_max if (f_max and 0 < f_max <= nyq) else nyq
    lo = f_min if f_min and f_min > 0 else 0.0
    if log_f:
        lo = max(lo, df)
    if lo >= hi:
        lo = df if log_f else 0.0
    return float(lo), float(hi)


# ==========================
# Спектрограмма из записанного файла (matplotlib)
# ==========================

def plot_spectrogram(times: np.ndarray, wl_nm: np.ndarray, settings: Dict,
                     pkt_ctr: Optional[np.ndarray] = None, title: str = '',
                     max_columns: int = 3000, max_trace_points: int = 20000):
    """
    Спектрограмма колебаний длины волны одной решётки (оконное Фурье).

    Фигура: сверху λ(t) (общая ось времени), снизу спектрограмма PSD(f, t)
    в dB re 1 pm²/Hz (или амплитуда в dB re 1 pm для шкалы «Amplitude»),
    справа — спектр в выбранной шкале и цветовая шкала. Правый график интерактивен:
    щелчок по спектрограмме (или λ(t)) — спектр одного кадра длиной T вокруг точки,
    протяжка мышью — спектр Уэлча по выделенному интервалу, правый щелчок — вся запись
    (при выборе вся запись остаётся серой пунктирной линией для сравнения).

    Возвращает (fig, info) — info: fs, Δf, nperseg, сегментов на столбец и т.п.
    """
    import matplotlib.pyplot as plt

    t = np.asarray(times, dtype=float)
    y_raw = np.asarray(wl_nm, dtype=float)
    if y_raw.size < 16:
        raise ValueError('Слишком мало точек для спектрального анализа')

    y_u, fs, uinfo = make_uniform(t, y_raw, pkt_ctr)
    y_u, bad = fill_nans(y_u)
    if bad > 0.5:
        raise ValueError(f'В ряду {100 * bad:.0f} % NaN — решётка почти не детектировалась')
    y_pm = y_u * NM_TO_PM

    T = float(settings['fft_window_s'])
    nperseg = int(round(T * fs))
    if nperseg > y_pm.size:
        nperseg = y_pm.size
    nperseg = max(16, nperseg)
    win_name = settings['window']
    scale = settings['scale']
    log_f = bool(settings['log_f'])

    t_c, f, psd, group = spectrogram_psd(y_pm, fs, nperseg, overlap=settings['overlap'],
                                         window=win_name, max_columns=max_columns)
    df = f[1] - f[0]
    f_lo, f_hi = freq_limits(settings['f_min_hz'], settings['f_max_hz'], fs, df, log_f)

    # средний спектр — Уэлч по всей записи (50 %)
    f_m, psd_m, nseg_m = welch_psd(y_pm, fs, nperseg, None, win_name, 0.5)
    rms = float(band_rms(f_m, psd_m, settings['f_min_hz'], f_hi))
    mean_vals = psd_to_scale(psd_m, scale, win_name, nperseg, fs)
    ylabel_mean, _qty, log_mean = scale_units(scale)

    if scale.startswith('Amplitude'):
        img = 20.0 * np.log10(np.maximum(psd_to_scale(psd, scale, win_name, nperseg, fs), 1e-15))
        cbar_label = 'Amplitude, dB re 1 pm'
    else:
        img = 10.0 * np.log10(np.maximum(psd, 1e-30))
        cbar_label = 'PSD, dB re 1 pm²/Hz'

    fmask = (f >= f_lo) & (f <= f_hi)
    if log_f:
        fmask &= f > 0
    img_v = img[fmask]
    f_v = f[fmask]
    finite = img_v[np.isfinite(img_v)]
    vmin, vmax = (np.percentile(finite, [1.0, 99.9]) if finite.size else (None, None))

    t_off = float(t[0]) if t.size else 0.0
    t_c = t_c + t_off

    from matplotlib.ticker import LogLocator, NullFormatter

    fig = plt.figure(figsize=(12.5, 7.5))
    gs = fig.add_gridspec(2, 3, width_ratios=[1.0, 0.24, 0.025], height_ratios=[1.0, 2.6],
                          wspace=0.05, hspace=0.06, left=0.08, right=0.91, top=0.88, bottom=0.08)
    ax_tr = fig.add_subplot(gs[0, 0])
    ax_sp = fig.add_subplot(gs[1, 0], sharex=ax_tr)
    ax_m = fig.add_subplot(gs[1, 1], sharey=ax_sp)
    cax = fig.add_subplot(gs[1, 2])

    # λ(t) с прореживанием по огибающей (на экране всё равно видно не больше пикселей)
    try:
        from FBGRecorder import _decimate_envelope
    except Exception:
        _decimate_envelope = None
    if _decimate_envelope is not None and y_raw.size > max_trace_points:
        tx, ty = _decimate_envelope(t, y_raw, max_trace_points)
    else:
        tx, ty = t, y_raw
    ax_tr.plot(tx, ty, lw=0.6)
    ax_tr.set_ylabel('λ, nm')
    ax_tr.tick_params(labelbottom=False)
    ax_tr.grid(alpha=0.3)
    ax_tr.ticklabel_format(axis='y', useOffset=False)

    if log_f:
        # неравномерная по оси частот картинка -> pcolormesh
        dt = (t_c[1] - t_c[0]) if t_c.size > 1 else nperseg / fs
        t_edges = np.concatenate([t_c - dt / 2, [t_c[-1] + dt / 2]])
        f_edges = np.concatenate([f_v - df / 2, [f_v[-1] + df / 2]])
        f_edges[0] = max(f_edges[0], f_v[0] / 2)
        im = ax_sp.pcolormesh(t_edges, f_edges, img_v, shading='flat', cmap='viridis',
                              vmin=vmin, vmax=vmax, rasterized=True)
        ax_sp.set_yscale('log')
        f_lo = max(f_lo, float(f_edges[0]))
    else:
        dt = (t_c[1] - t_c[0]) if t_c.size > 1 else nperseg / fs
        extent = [t_c[0] - dt / 2, t_c[-1] + dt / 2, f_v[0] - df / 2, f_v[-1] + df / 2]
        im = ax_sp.imshow(img_v, origin='lower', aspect='auto', extent=extent,
                          cmap='viridis', vmin=vmin, vmax=vmax, interpolation='nearest')
    ax_sp.set_ylim(f_lo, f_hi)
    ax_sp.set_xlabel('Time, s')
    ax_sp.set_ylabel('Frequency, Hz')
    ax_tr.set_xlim(float(t[0]), float(t[-1]))
    cb = fig.colorbar(im, cax=cax)
    cb.set_label(cbar_label)

    mm = (f_m >= f_lo) & (f_m <= f_hi) & ((f_m > 0) if log_f else True)
    # серая — вся запись (опорная), синяя — выбранный кадр/интервал
    line_all, = ax_m.plot(mean_vals[mm], f_m[mm], lw=0.7, color='0.6', ls='--',
                          label='whole record', visible=False)
    line_sel, = ax_m.plot(mean_vals[mm], f_m[mm], lw=0.8, color='C0', label='selection')
    if log_mean:
        ax_m.set_xscale('log')
        ax_m.xaxis.set_major_locator(LogLocator(numticks=4))
        ax_m.xaxis.set_minor_formatter(NullFormatter())
    ax_m.set_xlabel(ylabel_mean)
    ax_m.tick_params(labelleft=False)
    ax_m.grid(alpha=0.3, which='both')

    # ---------- выбор кадра / интервала для правого графика ----------
    y_n = y_pm.size
    f_min_band = settings['f_min_hz']

    def _spectrum_for(ta, tb):
        """Спектр по интервалу [ta, tb] (с); если он короче окна — один кадр длиной T вокруг середины."""
        if tb < ta:
            ta, tb = tb, ta
        i0 = int(round((ta - t_off) * fs))
        i1 = int(round((tb - t_off) * fs))
        if i1 - i0 < nperseg:
            ic = (i0 + i1) // 2
            i0 = ic - nperseg // 2
            i0 = min(max(0, i0), max(0, y_n - nperseg))
            i1 = i0 + nperseg
        i0, i1 = max(0, i0), min(y_n, i1)
        f_s, p_s, nseg = welch_psd(y_pm[i0:i1], fs, nperseg, None, win_name, 0.5)
        return f_s, p_s, nseg, t_off + i0 / fs, t_off + i1 / fs

    def _autoscale_m():
        xs = [ln.get_xdata() for ln in (line_sel, line_all) if ln.get_visible()]
        x = np.concatenate([np.asarray(v, dtype=float) for v in xs]) if xs else np.array([])
        x = x[np.isfinite(x)]
        if log_mean:
            x = x[x > 0]
        if x.size == 0:
            return
        lo, hi = float(x.min()), float(x.max())
        if log_mean:
            ax_m.set_xlim(lo / 1.5, hi * 1.5)
        else:
            pad = 0.05 * (hi - lo) if hi > lo else 1.0
            ax_m.set_xlim(lo - pad, hi + pad)

    sel = {'spans': [], 'press': None, 'cur': None}

    def _clear_spans():
        for art in sel['spans']:
            try:
                art.remove()
            except Exception:
                pass
        sel['spans'] = []

    def _draw_spans(ta, tb):
        _clear_spans()
        lo_, hi_ = min(ta, tb), max(ta, tb)
        sel['spans'] = [
            ax_sp.axvspan(lo_, hi_, facecolor='white', alpha=0.18, edgecolor='white', lw=1.2),
            ax_tr.axvspan(lo_, hi_, facecolor='C1', alpha=0.2, edgecolor='C1', lw=1.0),
        ]

    def select_interval(ta=None, tb=None):
        """Пересчитать правый график: None — вся запись; иначе интервал/кадр [ta, tb]."""
        if ta is None:
            _clear_spans()
            line_sel.set_xdata(mean_vals[mm])
            line_all.set_visible(False)
            ax_m.set_title(f'whole record\nrms = {rms:.4g} pm', fontsize=9)
            sel['cur'] = None
        else:
            f_s, p_s, nseg, a_, b_ = _spectrum_for(ta, tb)
            vals = psd_to_scale(p_s, scale, win_name, nperseg, fs)
            line_sel.set_xdata(vals[mm])
            line_all.set_visible(True)
            r = float(band_rms(f_s, p_s, f_min_band, f_hi))
            if nseg == 1:
                head = f't = {(a_ + b_) / 2:.3f} s  (1 frame)'
            else:
                head = f'{a_:.2f}–{b_:.2f} s  ({nseg} frames)'
            ax_m.set_title(f'{head}\nrms = {r:.4g} pm', fontsize=9)
            _draw_spans(a_, b_)
            sel['cur'] = (a_, b_)
        _autoscale_m()
        fig.canvas.draw_idle()

    def _toolbar_busy():
        tb = getattr(fig.canvas, 'toolbar', None)
        return bool(tb is not None and getattr(tb, 'mode', ''))

    def _on_press(ev):
        if ev.inaxes not in (ax_sp, ax_tr) or ev.xdata is None or _toolbar_busy():
            return
        if ev.button == 3:
            sel['press'] = None
            select_interval(None)
        elif ev.button == 1:
            sel['press'] = (ev.x, ev.xdata)

    def _on_motion(ev):
        if sel['press'] is None or ev.xdata is None or ev.inaxes not in (ax_sp, ax_tr):
            return
        _draw_spans(sel['press'][1], ev.xdata)
        fig.canvas.draw_idle()

    def _on_release(ev):
        if sel['press'] is None or ev.button != 1:
            return
        px0, x0 = sel['press']
        sel['press'] = None
        x1 = ev.xdata if (ev.xdata is not None and ev.inaxes in (ax_sp, ax_tr)) else x0
        if abs((ev.x if ev.x is not None else px0) - px0) < 4:
            select_interval(x0, x0)          # щелчок — один кадр
        else:
            select_interval(x0, x1)          # протяжка — интервал

    fig._acoustic_cids = [fig.canvas.mpl_connect('button_press_event', _on_press),
                          fig.canvas.mpl_connect('motion_notify_event', _on_motion),
                          fig.canvas.mpl_connect('button_release_event', _on_release)]
    fig._acoustic_select = select_interval      # ссылка (и доступ из кода/тестов)
    fig._acoustic_selection = sel
    fig.text(0.01, 0.012, 'Spectrogram / λ(t): click — spectrum of one frame;  drag — averaged over '
             'interval;  right click — whole record', fontsize=8, color='0.4')
    select_interval(None)

    sub = (f'fs ≈ {fs:.1f} Hz, T = {nperseg / fs:.3g} s (Δf = {df:.3g} Hz), {win_name}, '
           f'overlap {100 * settings["overlap"]:.0f} %'
           + (f', {group} segm./column' if group > 1 else '')
           + f';  rms({settings["f_min_hz"]:g}–{f_hi:.4g} Hz) = {rms:.4g} pm')
    fig.suptitle((title + '\n' if title else '') + sub, fontsize=10)

    info = {'fs': fs, 'df': df, 'nperseg': nperseg, 'group': group, 'n_columns': t_c.size,
            'rms_pm': rms, 'missing_restored': uinfo.get('missing', 0),
            'counter_used': uinfo.get('counter_used', False), 'nan_fraction': bad}
    return fig, info
