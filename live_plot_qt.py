# -*- coding: utf-8 -*-
"""
live_plot_qt.py — интерактивные live-графики на PyQt5 + pyqtgraph (v3.0.0).

Заменяют прежние matplotlib-окна для кнопок «Plot live dynamics» / «Plot live
spectra», а также для live-графика при записи и при динамических измерениях.

Публичный API (его вызывает AFR_interrogator.FBGRecorder; сигнатуры сохранены):
    live_plot_wavelengths(...)      -> (stop, window)
    start_live_spectra_session(...) -> (stop_all, info)
    live_plot_acoustic_spectra(...) -> (stop, window)        [v3.1.0]
    start_live_acoustic_session(...) -> (stop_all, info)     [v3.1.0]
    close_all_live_windows()

Все функции нужно вызывать из GUI-потока (там, где живёт QApplication).

Интерактивность (общая для всех окон):
    * колесо мыши           — масштаб (над осью — только по этой оси)
    * левая кнопка + drag   — панорамирование
    * правая кнопка + drag  — масштаб по осям
    * правый клик           — меню pyqtgraph (в т.ч. Export… в PNG/SVG/CSV)
    * «Pause / Resume»      — заморозить картинку (данные при этом копятся),
                              чтобы спокойно рассмотреть/увеличить участок
    * «Auto-scale / reset»  — вернуть автомасштаб и исходный диапазон
    * «Crosshair»           — перекрестие со считыванием координат
Закрытие окна крестиком останавливает его обновление и освобождает ресурсы.
После stop() (кнопка отжата) окно остаётся открытым: график «замирает»,
и его можно спокойно изучать/увеличивать/экспортировать.
"""

import queue
import threading
import time as _time
from typing import Optional

import numpy as np
from PyQt5 import QtCore, QtWidgets, QtGui
import pyqtgraph as pg

# Белый фон и тёмный передний план — как были у matplotlib-графиков.
pg.setConfigOptions(background="w", foreground="k")

# Окна держим в глобальном реестре: после stop() на них не остаётся других
# ссылок, а Qt-окно без ссылки на Python-обёртку было бы уничтожено.
_OPEN_WINDOWS = set()

# Палитра tab10 (как в matplotlib)
_PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
            "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


def close_all_live_windows() -> None:
    """Закрывает все открытые live-окна."""
    for w in list(_OPEN_WINDOWS):
        try:
            w.close()
        except Exception:
            pass
    _OPEN_WINDOWS.clear()


def _require_app() -> None:
    if QtWidgets.QApplication.instance() is None:
        raise RuntimeError("QApplication не создан: live-график нужно открывать из GUI-потока")


def _rec():
    """Модуль FBGRecorder (настройки LIVE_PLOT_* и вспомогательные классы)."""
    try:
        from . import FBGRecorder
    except ImportError:
        import FBGRecorder
    return FBGRecorder


# ==========================
# Окно и перекрестие
# ==========================

class LivePlotWindow(QtWidgets.QWidget):
    """Окно с панелью управления (Pause, Reset, Crosshair) и областью графиков."""

    closed = QtCore.pyqtSignal()

    def __init__(self, title: str, size=(1100, 650)):
        super().__init__()
        self._base_title = str(title)
        self.setWindowTitle(self._base_title)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.resize(int(size[0]), int(size[1]))

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)

        bar = QtWidgets.QHBoxLayout()
        root.addLayout(bar)
        self.bar = bar

        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip("Заморозить картинку (данные продолжают накапливаться)")
        self.btn_reset = QtWidgets.QPushButton("Auto-scale / reset view")
        self.btn_reset.setToolTip("Вернуть автомасштаб и исходный диапазон осей")
        self.chk_cross = QtWidgets.QCheckBox("Crosshair")
        self.chk_cross.setChecked(True)
        self.readout = QtWidgets.QLabel("")
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.readout.setFont(mono)
        self.readout.setMinimumWidth(300)
        self.readout.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        bar.addWidget(self.btn_pause)
        bar.addWidget(self.btn_reset)
        bar.addWidget(self.chk_cross)
        bar.addStretch(1)
        bar.addWidget(self.readout)

        self.btn_pause.toggled.connect(
            lambda on: self.btn_pause.setText("Resume" if on else "Pause"))

        self.body = QtWidgets.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.body, 1)

        _OPEN_WINDOWS.add(self)

    @property
    def paused(self) -> bool:
        return self.btn_pause.isChecked()

    def mark_stopped(self) -> None:
        try:
            self.setWindowTitle(self._base_title + "  [stopped]")
            self.btn_pause.setChecked(False)
            self.btn_pause.setEnabled(False)
        except RuntimeError:   # окно уже удалено
            pass

    def closeEvent(self, ev):
        _OPEN_WINDOWS.discard(self)
        try:
            self.closed.emit()
        except Exception:
            pass
        super().closeEvent(ev)


class _Crosshair:
    """Перекрестие + считывание координат под курсором для набора PlotItem."""

    def __init__(self, plots, window: LivePlotWindow, fmt):
        self.plots = list(plots)
        self.window = window
        self.fmt = fmt
        self.v = {}
        self.h = {}
        pen = pg.mkPen("#666666", width=1, style=QtCore.Qt.DashLine)
        for p in self.plots:
            v = pg.InfiniteLine(angle=90, movable=False, pen=pen)
            h = pg.InfiniteLine(angle=0, movable=False, pen=pen)
            v.setVisible(False)
            h.setVisible(False)
            p.addItem(v, ignoreBounds=True)
            p.addItem(h, ignoreBounds=True)
            self.v[id(p)] = v
            self.h[id(p)] = h
        self._proxy = pg.SignalProxy(self.plots[0].scene().sigMouseMoved,
                                     rateLimit=60, slot=self._moved)

    def _hide_all(self):
        for p in self.plots:
            self.v[id(p)].setVisible(False)
            self.h[id(p)].setVisible(False)

    def _moved(self, evt):
        pos = evt[0]
        if not self.window.chk_cross.isChecked():
            self._hide_all()
            return
        hit = None
        for p in self.plots:
            if p.sceneBoundingRect().contains(pos):
                hit = p
                break
        if hit is None:
            self._hide_all()
            return
        mp = hit.vb.mapSceneToView(pos)
        x, y = float(mp.x()), float(mp.y())
        for p in self.plots:
            # вертикальная линия — во всех подграфиках (общая ось X), горизонтальная — только в своём
            self.v[id(p)].setVisible(True)
            self.v[id(p)].setPos(x)
            self.h[id(p)].setVisible(p is hit)
        self.h[id(hit)].setPos(y)
        try:
            self.window.readout.setText(self.fmt(hit, x, y))
        except Exception:
            pass


def _style_plot(p, xlabel=None, ylabel=None):
    p.showGrid(x=True, y=True, alpha=0.25)
    p.hideButtons()
    p.disableAutoRange()          # диапазоны задаём сами (см. автомасштаб ниже)
    for side in ("left", "bottom"):
        ax = p.getAxis(side)
        ax.enableAutoSIPrefix(False)      # без «k»-приставок: длины волн ~1550.xxxx нм
    if xlabel:
        p.setLabel("bottom", xlabel)
    if ylabel:
        p.setLabel("left", ylabel)
    p.getAxis("left").setWidth(75)


# ==========================
# Live-график длин волн FBG во времени
# ==========================

def live_plot_wavelengths(it,
                          channel: int,
                          fbg_indices,
                          window_sec: float = 20.0,
                          expected_rate_hz: float = 2000.0,
                          max_fps: int = 30,
                          title: Optional[str] = None,
                          ylim=None,
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
    Интерактивный live-график длин волн решёток выбранного канала.

    Источник данных — source_queue с элементами
        (t_perf, wl_full) ИЛИ (t_perf, ts_unix, pkt_ctr, wl_full)
    (функция НЕ читает it.pop_freq_frame, это делает FrameFanout).

    Возвращает (stop, window): window — LivePlotWindow (QWidget).

    Параметры те же, что у прежней matplotlib-версии; use_blit оставлен для
    совместимости и игнорируется (pyqtgraph блиттинг не нужен).

    Автомасштаб по Y — с гистерезисом раз в autoscale_every тиков. Как только
    пользователь сам подвинул/увеличил график мышью, автомасштаб этого
    подграфика отключается до нажатия «Auto-scale / reset view».
    """
    _require_app()
    rec = _rec()

    if source_queue is None:
        raise ValueError(
            "live_plot_wavelengths работает только от source_queue. "
            "Создайте FrameFanout и передайте очередь."
        )
    q = source_queue

    n_ch = int(getattr(it, "channels", 0) or 0)
    fbg_per_ch = int(getattr(it, "fbg_per_ch", 0) or 0)
    if n_ch > 0 and not (1 <= int(channel) <= n_ch):
        raise ValueError(f"Некорректный channel={channel}, допустимо 1..{n_ch}")
    channel_0 = int(channel) - 1

    fbg_indices = [int(i) for i in list(fbg_indices)]
    if fbg_per_ch > 0:
        for i in fbg_indices:
            if not (0 <= i < fbg_per_ch):
                raise ValueError(f"Некорректный индекс решётки {i}, допустимо 0..{fbg_per_ch - 1}")

    n_series = len(fbg_indices)
    window_sec = float(window_sec)

    # Ёмкость буфера — по максимально возможной частоте потока (см. прежнюю версию)
    capacity = max(1000, int(max(float(expected_rate_hz), 8000.0) * window_sec * 1.2))
    buf = rec._ScrollBuffer(max(1, n_series), capacity)

    win_title = title or f"Channel {channel}"
    win = LivePlotWindow(win_title, size=(1100, min(max(380, 230 * max(1, n_series) + 80), 900)))
    if geometry is not None:
        win.setGeometry(geometry)

    head = QtWidgets.QLabel(win_title)
    f = head.font()
    f.setBold(True)
    head.setFont(f)
    head.setAlignment(QtCore.Qt.AlignHCenter)
    win.body.addWidget(head)

    glw = pg.GraphicsLayoutWidget()
    win.body.addWidget(glw, 1)

    xlabel = "Time relative to now, s" if relative_time else "Time, s"
    ylabel = "FBG wavelength, nm"
    aa = bool(getattr(rec, "LIVE_PLOT_ANTIALIASED", True))

    plots = []            # уникальные PlotItem
    curves = []           # по одной кривой на решётку (порядок = fbg_indices)
    plot_of = {}          # fbg -> PlotItem
    color_of = {fbg: _PALETTE[k % len(_PALETTE)] for k, fbg in enumerate(fbg_indices)}

    if use_subplots and n_series > 0:
        for k, fbg in enumerate(fbg_indices):
            p = glw.addPlot(row=k, col=0)
            is_last = (k == n_series - 1)
            _style_plot(p, xlabel if is_last else None, ylabel)
            if not is_last:
                p.getAxis("bottom").setStyle(showValues=False)
            p.setTitle(f"FBG {fbg + 1}", size="9pt")
            if k > 0:
                p.setXLink(plots[0])
            plots.append(p)
            plot_of[fbg] = p
            c = p.plot([], [], pen=pg.mkPen(color_of[fbg], width=1), antialias=aa,
                       autoDownsample=True, clipToView=True)
            curves.append(c)
    else:
        p = glw.addPlot(row=0, col=0)
        _style_plot(p, xlabel, ylabel)
        p.addLegend(offset=(-10, 10))
        plots.append(p)
        for fbg in fbg_indices:
            plot_of[fbg] = p
            c = p.plot([], [], pen=pg.mkPen(color_of[fbg], width=1), antialias=aa,
                       name=f"FBG {fbg + 1}", autoDownsample=True, clipToView=True)
            curves.append(c)

    series_pos = {f_: k for k, f_ in enumerate(fbg_indices)}

    def _reset_x():
        if relative_time:
            plots[0].setXRange(-window_sec, 0.0, padding=0)

    _reset_x()

    # Состояние автомасштаба: по подграфику
    auto_y = {id(p): (ylim is None) for p in plots}
    if ylim is not None:
        for p in plots:
            p.setYRange(float(ylim[0]), float(ylim[1]), padding=0)
    state = {"t0": None, "tick": 0, "work_ms": 0.0, "need_ylim": ylim is None,
             "interval": None, "closed": False, "follow_x": True}

    def _on_manual(p):
        def _h(*_a):
            auto_y[id(p)] = False
            if not relative_time:
                state["follow_x"] = False
        return _h
    for p in plots:
        p.vb.sigRangeChangedManually.connect(_on_manual(p))

    shift_artists = {}

    def _update_shift_lines(x_text):
        if shift_provider is None:
            return
        data = shift_provider()
        if not data:
            return
        for fbg, value in data.items():
            if fbg not in plot_of:
                continue
            try:
                initial_wl, shift = value
            except (TypeError, ValueError):
                continue
            p = plot_of[fbg]
            color = color_of.get(fbg, _PALETTE[0])
            art = shift_artists.get(fbg)
            if art is None:
                pen = pg.mkPen(color, width=1, style=QtCore.Qt.DashLine)
                h_init = pg.InfiniteLine(pos=initial_wl, angle=0, movable=False, pen=pen)
                h_max = pg.InfiniteLine(pos=initial_wl + shift, angle=0, movable=False, pen=pen)
                txt = pg.TextItem(f"shift={shift * 1e3:.1f} pm", color=color, anchor=(0.5, 0.5))
                txt.setPos(x_text, initial_wl + shift / 2)
                for a in (h_init, h_max, txt):
                    p.addItem(a, ignoreBounds=True)
                shift_artists[fbg] = (h_init, h_max, txt)
            else:
                h_init, h_max, txt = art
                h_init.setValue(initial_wl)
                h_max.setValue(initial_wl + shift)
                txt.setPos(x_text, initial_wl + shift / 2)
                txt.setText(f"shift={shift * 1e3:.1f} pm")

    def _consume_batch():
        items = []
        for _ in range(int(max_frames_per_update)):
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        if not items:
            return 0
        if state["t0"] is None:
            state["t0"] = float(items[0][0])

        t_list = [it_[0] for it_ in items]
        try:
            rows = []
            for it_ in items:
                wl = it_[1] if len(it_) == 2 else it_[3]
                row_ch = wl[channel_0]
                rows.append([row_ch[i] for i in fbg_indices])
            Y = np.asarray(rows, dtype=np.float64)
        except Exception:
            rows = []
            for it_ in items:
                wl = it_[1] if len(it_) == 2 else it_[3]
                row_ch = []
                if isinstance(wl, (list, tuple)) and len(wl) > channel_0:
                    row_ch = wl[channel_0]
                r = []
                for i in fbg_indices:
                    try:
                        r.append(float(row_ch[i]))
                    except Exception:
                        r.append(float("nan"))
                rows.append(r)
            Y = np.asarray(rows, dtype=np.float64)

        if Y.ndim != 2 or Y.shape[1] != n_series:
            return 0
        buf.append(np.asarray(t_list, dtype=np.float64), np.ascontiguousarray(Y.T))
        return len(items)

    def _autoscale_y(Y_win, force=False):
        groups = {}
        for f_ in fbg_indices:
            pl = plot_of[f_]
            groups.setdefault(id(pl), (pl, []))[1].append(f_)
        for pl, fbgs in groups.values():
            if not auto_y.get(id(pl), True) and not force:
                continue
            lo, hi = np.inf, -np.inf
            for f_ in fbgs:
                y = Y_win[series_pos[f_]]
                y = y[np.isfinite(y)]
                if y.size:
                    lo = min(lo, float(y.min()))
                    hi = max(hi, float(y.max()))
                art = shift_artists.get(f_)
                if art is not None:
                    for h in art[:2]:
                        yy = float(h.value())
                        lo, hi = min(lo, yy), max(hi, yy)
            if not np.isfinite(lo) or not np.isfinite(hi):
                continue
            span = hi - lo
            pad = 0.08 * span if span > 0 else max(abs(lo) * 1e-6, 1e-6)
            tgt_lo, tgt_hi = lo - pad, hi + pad
            cur_lo, cur_hi = pl.vb.viewRange()[1]
            cur_span = cur_hi - cur_lo
            # гистерезис: меняем, только если данные вылезли или занимают < 40 % оси
            if (force or lo < cur_lo or hi > cur_hi or (tgt_hi - tgt_lo) < 0.4 * cur_span):
                pl.setYRange(tgt_lo, tgt_hi, padding=0)

    base_interval_ms = max(1, int(1000 / max(1, int(max_fps))))
    timer = QtCore.QTimer()
    timer.setInterval(base_interval_ms)
    state["interval"] = base_interval_ms
    max_points_cap = int(max_plot_points) * 20 if max_plot_points and max_plot_points > 0 else 0

    def _adapt_interval():
        want = int(min(500, max(base_interval_ms, 3.0 * state["work_ms"])))
        cur = state["interval"]
        if cur is None or abs(want - cur) > max(5, 0.2 * cur):
            state["interval"] = want
            timer.setInterval(want)

    def tick(*_args):
        if state["closed"]:
            return
        t_a = _time.perf_counter()
        state["tick"] += 1
        _consume_batch()          # данные копим и на паузе — чтобы после Resume не было дыр

        if win.paused or state["t0"] is None or buf.n == 0:
            return

        t_now = float(buf.t[buf.n - 1])
        t_cut = t_now - window_sec if window_sec > 0 else float(buf.t[0])
        t_win, Y_win = buf.window(t_cut)
        if t_win.size == 0:
            return

        # Страховка от слишком больших окон. Основную децимацию (min/max по пикселям)
        # делает pyqtgraph и пересчитывает её при увеличении, так что детали сохраняются.
        Xd, Yd = rec._decimate_envelope_multi(t_win, Y_win, max_points_cap)

        t_ref = t_now if relative_time else float(state["t0"])
        for k, c in enumerate(curves):
            c.setData(np.asarray(Xd[k]) - t_ref, np.array(Yd[k], dtype=np.float64))

        if relative_time:
            x_text = -window_sec / 2.0
        else:
            t_min = float(t_win[0]) - t_ref
            t_max = max(t_now - t_ref, t_min + 1e-3)
            if state["follow_x"]:
                plots[0].setXRange(t_min, t_max, padding=0)
            x_text = (t_min + t_max) / 2
        _update_shift_lines(x_text)

        if ylim is None:
            if state["need_ylim"] or state["tick"] % int(max(1, autoscale_every)) == 0:
                _autoscale_y(np.asarray(Yd), force=state["need_ylim"])
                state["need_ylim"] = False

        state["work_ms"] = (_time.perf_counter() - t_a) * 1e3
        _adapt_interval()

    timer.timeout.connect(tick)

    def reset_view():
        state["follow_x"] = True
        for p in plots:
            auto_y[id(p)] = (ylim is None)
        _reset_x()
        if ylim is not None:
            for p in plots:
                p.setYRange(float(ylim[0]), float(ylim[1]), padding=0)
        elif buf.n:
            t_now = float(buf.t[buf.n - 1])
            _, Y_win = buf.window(t_now - window_sec)
            if Y_win.shape[1]:
                _autoscale_y(np.asarray(Y_win), force=True)

    win.btn_reset.clicked.connect(lambda *_: reset_view())

    def _fmt(plot, x, y):
        name = ""
        for f_, pl in plot_of.items():
            if pl is plot and use_subplots:
                name = f"FBG {f_ + 1}  "
                break
        tx = f"t={x:+.3f} s" if relative_time else f"t={x:.3f} s"
        return f"{name}{tx}   λ={y:.5f} nm"

    _Crosshair(plots, win, _fmt)

    def stop():
        try:
            timer.stop()
        except Exception:
            pass
        try:
            win.mark_stopped()
        except Exception:
            pass

    def _on_closed():
        state["closed"] = True
        try:
            timer.stop()
        except Exception:
            pass
    win.closed.connect(_on_closed)

    win._live_timer = timer           # ссылка, чтобы таймер не собрался GC
    win._live_tick = tick             # для тестов/ручного вызова
    timer.start()
    win.show()

    if blocking:
        loop = QtCore.QEventLoop()
        win.closed.connect(loop.quit)
        loop.exec_()

    return stop, win


# ==========================
# Live-спектры отражения
# ==========================

def start_live_spectra_session(it,
                               channels,
                               waves=None,
                               interval_ms: int = 80,
                               title_prefix: str = "Live spectra",
                               dot_color: str = "red",
                               markersize: float = 8.0,
                               on_error=None,
                               on_status=None):
    """
    Интерактивные live-спектры отражения: по одному окну на выбранный канал.

    Фоновый поток последовательно запрашивает у интеррогатора пики FBG
    (get_single_FBG_measurement) и спектр каждого канала (get_single_spectrum);
    QTimer в GUI-потоке обновляет кривую спектра и крупные точки пиков.

    Автомасштаб по Y выполняется один раз — при получении первых данных, дальше
    ось фиксирована (её можно менять мышью; «Auto-scale / reset view» — вернуть).

    Возвращает (stop_all, info).
    """
    _require_app()

    if channels is None or len(channels) == 0:
        raise ValueError("channels должен быть непустым списком (1-based)")
    channels = [int(c) for c in channels]

    if waves is None:
        waves = it.get_waves()
    waves = np.asarray(waves, dtype=float)

    def _interp(px, x, y):
        """Интерполяция y(px) по x; корректно работает и для убывающего x."""
        if x.size < 2:
            return np.full(len(px), np.nan)
        if x[0] > x[-1]:
            xr, yr = x[::-1], y[::-1]
        else:
            xr, yr = x, y
        return np.interp(px, xr, yr)

    lock = threading.Lock()
    latest = {ch: None for ch in channels}
    stop_event = threading.Event()

    x0 = float(np.nanmin(waves))
    x1 = float(np.nanmax(waves))
    aa = bool(getattr(_rec(), "LIVE_PLOT_ANTIALIASED", True))

    wins = {}
    curves = {}
    dots = {}
    plots = {}
    autoscaled = {ch: False for ch in channels}
    sym_size = max(4, int(round(float(markersize) * 1.3)))

    for ch in channels:
        win = LivePlotWindow(f"{title_prefix} (ch {ch})", size=(1000, 520))
        glw = pg.GraphicsLayoutWidget()
        win.body.addWidget(glw, 1)
        p = glw.addPlot(row=0, col=0)
        _style_plot(p, "Wavelength, nm", "Spectral power, dBm")
        p.setTitle(f"{title_prefix} (ch {ch})", size="10pt")
        if x1 > x0:
            p.setXRange(x0, x1, padding=0)
        curve = p.plot([], [], pen=pg.mkPen(_PALETTE[0], width=1), antialias=aa)
        dot = p.plot([], [], pen=None, symbol="o", symbolSize=sym_size,
                     symbolBrush=pg.mkBrush(dot_color), symbolPen=None)
        wins[ch], curves[ch], dots[ch], plots[ch] = win, curve, dot, p

        def _fmt(plot, x, y):
            return f"λ={x:.4f} nm   P={y:.2f} dBm"
        _Crosshair([p], win, _fmt)

    # --- фоновый поток опроса интеррогатора ---
    def _worker():
        last_err_report = 0.0
        first_data_logged = {ch: False for ch in channels}
        iter_count = 0

        def _report(msg):
            nonlocal last_err_report
            if not on_error:
                return
            now = _time.monotonic()
            if (now - last_err_report) > 1.0:
                last_err_report = now
                try:
                    on_error(msg)
                except Exception:
                    pass

        while not stop_event.is_set():
            for ch in channels:
                if stop_event.is_set():
                    break
                try:
                    spec = it.get_single_spectrum(ch)
                except Exception as e:
                    _report(f"Spectrum acquisition failed (ch {ch}): {e}")
                    continue
                if spec is None:
                    continue
                with lock:
                    prev = latest[ch]
                    peaks = prev[1] if prev is not None else None
                    latest[ch] = (np.asarray(spec, dtype=float), peaks)

                if not first_data_logged[ch]:
                    first_data_logged[ch] = True
                    if on_status:
                        try:
                            s = np.asarray(spec, dtype=float)
                            on_status(f"Live spectra (ch {ch}): {len(spec)} points, "
                                      f"{int(np.isfinite(s).sum())} finite")
                        except Exception:
                            pass

            if iter_count % 3 == 0 and not stop_event.is_set():
                try:
                    fbgs = it.get_single_FBG_measurement()
                except Exception as e:
                    fbgs = None
                    _report(f"FBG peaks acquisition failed: {e}")
                if fbgs is not None:
                    for ch in channels:
                        try:
                            peaks = [float(x) for x in fbgs[ch - 1] if not np.isnan(x)]
                        except Exception:
                            peaks = None
                        with lock:
                            prev = latest[ch]
                            spec = prev[0] if prev is not None else None
                            latest[ch] = (spec, peaks)
            iter_count += 1

    thr = threading.Thread(target=_worker, name="Spectra-Live", daemon=True)
    thr.start()

    # --- обновление графиков в GUI-потоке ---
    def _autoscale(ch, y, force=False):
        finite_y = y[np.isfinite(y)]
        if finite_y.size:
            ylo, yhi = float(finite_y.min()), float(finite_y.max())
            if yhi > ylo:
                pad = (yhi - ylo) * 0.05
                plots[ch].setYRange(ylo - pad, yhi + pad, padding=0)
                autoscaled[ch] = True

    def _update(ch):
        with lock:
            item = latest[ch]
        if item is None:
            return
        spec, peaks = item
        if spec is None:
            return
        n = min(len(spec), len(waves))
        x = waves[:n]
        y = spec[:n]
        curves[ch].setData(x, y)

        px = np.empty(0)
        if peaks:
            x_min, x_max = float(np.nanmin(x)), float(np.nanmax(x))
            px = np.asarray([p_ for p_ in peaks if x_min <= p_ <= x_max], dtype=float)
        if px.size:
            dots[ch].setData(px, _interp(px, x, y))
        else:
            dots[ch].setData([], [])

        if not autoscaled[ch]:
            _autoscale(ch, y)

    alive = set(channels)
    timer = QtCore.QTimer()
    timer.setInterval(int(interval_ms))

    def _tick():
        for ch in list(alive):
            if not wins[ch].paused:
                try:
                    _update(ch)
                except Exception as e:
                    if on_error:
                        try:
                            on_error(f"Live spectra draw error (ch {ch}): {e}")
                        except Exception:
                            pass
    timer.timeout.connect(_tick)

    def _make_reset(ch):
        def _reset(*_a):
            plots[ch].setXRange(x0, x1, padding=0)
            with lock:
                item = latest[ch]
            if item is not None and item[0] is not None:
                _autoscale(ch, item[0][:len(waves)], force=True)
        return _reset

    def _make_closed(ch):
        def _closed():
            alive.discard(ch)
            if not alive:
                stop_event.set()
                try:
                    timer.stop()
                except Exception:
                    pass
        return _closed

    for ch in channels:
        wins[ch].btn_reset.clicked.connect(_make_reset(ch))
        wins[ch].closed.connect(_make_closed(ch))
        wins[ch].show()

    timer.start()

    def stop_all():
        stop_event.set()
        try:
            thr.join(timeout=2.0)
        except Exception:
            pass
        try:
            timer.stop()
        except Exception:
            pass
        for w in list(wins.values()):
            try:
                w.close()
            except Exception:
                pass

    info = {
        "windows": list(wins.values()),
        "figs": list(wins.values()),       # совместимость со старым ключом
        "timer": timer,
        "thread": thr,
        "channels": channels,
    }
    return stop_all, info


# ==========================
# Live-спектры акустических шумов (Фурье колебаний длины волны FBG)
# ==========================

def _acoustic():
    try:
        from . import acoustic_analysis
    except ImportError:
        import acoustic_analysis
    return acoustic_analysis


def live_plot_acoustic_spectra(channel: int,
                               fbg_indices,
                               source_queue,
                               settings: dict,
                               expected_rate_hz: float = 2000.0,
                               title: Optional[str] = None,
                               on_error=None,
                               max_frames_per_update: int = 200000):
    """
    Интерактивный live-график спектров акустических шумов решёток одного канала:
    по подграфику на решётку, спектр колебаний λ(t), посчитанный методом Уэлча
    по скользящему буферу последних n_averages окон длиной fft_window_s
    (перекрытие 50 %, линейный детренд в каждом окне).

    source_queue — очередь FrameFanout с элементами (t_perf, ts_unix, pkt_ctr, wl_full).
    fbg_indices  — 0-based индексы решёток в канале.
    settings     — dict из acoustic_analysis.settings_from_params().

    В окне: Pause, Auto-scale/reset, Crosshair (как у остальных live-окон),
    плюс «Peak hold» (огибающая максимумов) и «Clear hold».
    В заголовке каждого подграфика — СКО колебаний λ в полосе [f_min, f_max].

    Возвращает (stop, window).
    """
    _require_app()
    rec = _rec()
    ac = _acoustic()

    channel_0 = int(channel) - 1
    fbg_indices = [int(i) for i in list(fbg_indices)]
    n_series = len(fbg_indices)
    if n_series == 0:
        raise ValueError(f"Для канала {channel} не выбрано ни одной решётки")
    q = source_queue

    T = float(settings['fft_window_s'])
    n_avg = int(settings['n_averages'])
    win_name = settings['window']
    scale = settings['scale']
    log_f = bool(settings['log_f'])
    f_min_set = float(settings['f_min_hz'])
    f_max_set = float(settings['f_max_hz'])
    ylabel, qty, log_y = ac.scale_units(scale)
    y_unit = ylabel.split(', ', 1)[1] if ', ' in ylabel else ''

    # Буфер: длительность Уэлча (T·(n_avg+1)/2) с запасом, по макс. возможной частоте.
    need_sec = T * (n_avg + 1) / 2.0
    capacity = int(max(4000, max(float(expected_rate_hz), 8000.0) * (need_sec + 2.0) * 1.3))
    buf = rec._ScrollBuffer(n_series + 1, capacity)    # последняя строка — счётчик пакетов

    win_title = title or f"Acoustic spectra (ch {channel})"
    win = LivePlotWindow(win_title, size=(1100, min(max(420, 240 * n_series + 110), 950)))

    chk_hold = QtWidgets.QCheckBox("Peak hold")
    chk_hold.setToolTip("Показывать огибающую максимумов спектра (серым)")
    btn_clear = QtWidgets.QPushButton("Clear hold")
    win.bar.insertWidget(3, chk_hold)
    win.bar.insertWidget(4, btn_clear)

    head = QtWidgets.QLabel(win_title)
    fh = head.font()
    fh.setBold(True)
    head.setFont(fh)
    head.setAlignment(QtCore.Qt.AlignHCenter)
    win.body.addWidget(head)
    status = QtWidgets.QLabel("waiting for data…")
    status.setAlignment(QtCore.Qt.AlignHCenter)
    win.body.addWidget(status)

    glw = pg.GraphicsLayoutWidget()
    win.body.addWidget(glw, 1)
    aa_ = bool(getattr(rec, "LIVE_PLOT_ANTIALIASED", True))

    plots, curves, holds = [], [], []
    for k, fbg in enumerate(fbg_indices):
        p = glw.addPlot(row=k, col=0)
        is_last = (k == n_series - 1)
        _style_plot(p, "Frequency, Hz" if is_last else None, ylabel)
        p.setLogMode(x=log_f, y=log_y)
        if not is_last:
            p.getAxis("bottom").setStyle(showValues=False)
        p.setTitle(f"FBG {fbg + 1}", size="9pt")
        if k > 0:
            p.setXLink(plots[0])
        color = _PALETTE[k % len(_PALETTE)]
        h = p.plot([], [], pen=pg.mkPen("#aaaaaa", width=1), antialias=aa_)
        c = p.plot([], [], pen=pg.mkPen(color, width=1), antialias=aa_)
        plots.append(p)
        curves.append(c)
        holds.append(h)

    state = {"closed": False, "t0": None, "fs": None, "x_set": False,
             "hold": [None] * n_series, "f": None, "last_err": 0.0}
    auto_y = {id(p): True for p in plots}

    def _on_manual(p):
        def _h(*_a):
            auto_y[id(p)] = False
        return _h
    for p in plots:
        p.vb.sigRangeChangedManually.connect(_on_manual(p))

    def _vx(x):
        return np.log10(x) if log_f else x

    def _vy(y):
        return np.log10(y) if log_y else y

    def _set_x(lo, hi):
        plots[0].setXRange(_vx(lo), _vx(hi), padding=0)

    def _autoscale(p, ys, force=False):
        y = np.concatenate([np.asarray(v, dtype=float) for v in ys if v is not None and len(v)])
        y = y[np.isfinite(y)]
        if log_y:
            y = y[y > 0]
        if y.size == 0:
            return
        vy = _vy(y)
        lo, hi = float(np.percentile(vy, 0.5)), float(vy.max())
        span = hi - lo
        pad = 0.06 * span if span > 0 else 0.5
        lo, hi = lo - pad, hi + pad
        cur_lo, cur_hi = p.vb.viewRange()[1]
        if force or lo < cur_lo or hi > cur_hi or (hi - lo) < 0.5 * (cur_hi - cur_lo):
            p.setYRange(lo, hi, padding=0)

    def _consume():
        items = []
        for _ in range(int(max_frames_per_update)):
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        if not items:
            return
        rows, ts = [], []
        for it_ in items:
            try:
                if len(it_) == 4:
                    t_, _u, ctr, wl = it_
                else:
                    t_, wl = it_
                    ctr = np.nan
                row_ch = wl[channel_0]
                r = []
                for i in fbg_indices:
                    try:
                        r.append(float(row_ch[i]))
                    except Exception:
                        r.append(np.nan)
                r.append(float(ctr))
                rows.append(r)
                ts.append(float(t_))
            except Exception:
                continue
        if not rows:
            return
        if state["t0"] is None:
            state["t0"] = ts[0]
        buf.append(np.asarray(ts, dtype=np.float64), np.ascontiguousarray(np.asarray(rows).T))

    def _report(msg):
        now = _time.monotonic()
        if on_error and now - state["last_err"] > 2.0:
            state["last_err"] = now
            try:
                on_error(msg)
            except Exception:
                pass

    def _compute():
        n = buf.n
        if n < 32:
            return
        t_all = buf.t[:n]
        # fs — по последним ≤ 10 с (средняя частота приёма кадров)
        i_fs = int(np.searchsorted(t_all, t_all[-1] - 10.0))
        span = float(t_all[-1] - t_all[i_fs])
        fs_est = (n - 1 - i_fs) / span if span > 0.5 else float(expected_rate_hz)
        nperseg = max(16, int(round(T * fs_est)))
        hop = max(1, nperseg // 2)
        n_need = nperseg + (n_avg - 1) * hop
        if n < nperseg:
            status.setText(f"filling buffer: {100.0 * n / nperseg:.0f} % of the first FFT window "
                           f"(fs ≈ {fs_est:.0f} Hz)")
            return
        i0 = max(0, n - n_need)
        t_w = buf.t[i0:n]
        Y_w = buf.y[:n_series, i0:n]
        ctr = buf.y[n_series, i0:n]
        Yu, fs, info = ac.make_uniform(t_w, Y_w, ctr, fs_hint=fs_est)
        Yu, bad = ac.fill_nans(Yu)
        Yu = Yu * ac.NM_TO_PM
        nps = min(nperseg, Yu.shape[1])
        f, psd, nseg = ac.welch_psd(Yu, fs, nps, n_averages=n_avg, window=win_name, overlap=0.5)
        vals = ac.psd_to_scale(psd, scale, win_name, nps, fs)
        df = f[1] - f[0] if f.size > 1 else 1.0
        f_lo, f_hi = ac.freq_limits(f_min_set, f_max_set, fs, df, log_f)
        rms = ac.band_rms(f, psd, f_min_set, f_hi)

        m = f > 0 if log_f else np.ones_like(f, dtype=bool)
        fx = f[m]
        if state["f"] is None or state["f"].shape != f.shape or not np.allclose(state["f"], f):
            state["hold"] = [None] * n_series        # сетка частот изменилась — сброс hold
        state["f"] = f

        for k in range(n_series):
            yk = vals[k][m]
            if log_y:
                yk = np.maximum(yk, 1e-12)
            curves[k].setData(fx, yk)
            if chk_hold.isChecked():
                hk = state["hold"][k]
                state["hold"][k] = yk.copy() if hk is None or hk.shape != yk.shape else np.maximum(hk, yk)
                holds[k].setData(fx, state["hold"][k])
            else:
                holds[k].setData([], [])
            plots[k].setTitle(f"FBG {fbg_indices[k] + 1}    rms({f_min_set:g}–{f_hi:.4g} Hz) = "
                              f"{rms[k]:.4g} pm", size="9pt")

        if not state["x_set"]:
            _set_x(f_lo, f_hi)
            state["x_set"] = True
        for k, p in enumerate(plots):
            if auto_y[id(p)]:
                _autoscale(p, [vals[k][m][(fx >= f_lo) & (fx <= f_hi)]],
                           force=(state["fs"] is None))
        state["fs"] = fs

        gaps = ""
        if info.get("counter_used") and info.get("missing"):
            gaps = f" | restored {info['missing']} lost frames"
        if bad > 0:
            gaps += f" | NaN {100 * bad:.1f} %"
        fill = "" if nseg >= n_avg else f" (buffer filling)"
        status.setText(f"fs ≈ {fs:.1f} Hz | T = {nps / fs:.3g} s, Δf = {df:.3g} Hz | {win_name} | "
                       f"averages {nseg}/{n_avg}{fill}{gaps}")

    def tick():
        if state["closed"]:
            return
        try:
            _consume()                      # копим и на паузе
            if win.paused:
                return
            _compute()
        except Exception as e:
            _report(f"Acoustic spectra error (ch {channel}): {e!r}")

    timer = QtCore.QTimer()
    timer.setInterval(int(settings.get('update_ms', 250)))
    timer.timeout.connect(tick)

    def reset_view():
        for p in plots:
            auto_y[id(p)] = True
        state["x_set"] = False
        state["fs"] = None
        if state["f"] is not None and not win.paused:
            tick()
    win.btn_reset.clicked.connect(lambda *_: reset_view())

    def clear_hold():
        state["hold"] = [None] * n_series
        for h in holds:
            h.setData([], [])
    btn_clear.clicked.connect(lambda *_: clear_hold())
    chk_hold.toggled.connect(lambda on: None if on else clear_hold())

    def _fmt(plot, x, y):
        fx = 10 ** x if log_f else x
        yy = 10 ** y if log_y else y
        name = ""
        for k, p in enumerate(plots):
            if p is plot:
                name = f"FBG {fbg_indices[k] + 1}  "
        return f"{name}f={fx:.4g} Hz   {qty}={yy:.4g} {y_unit}"
    _Crosshair(plots, win, _fmt)

    def stop():
        try:
            timer.stop()
        except Exception:
            pass
        try:
            win.mark_stopped()
        except Exception:
            pass

    def _on_closed():
        state["closed"] = True
        try:
            timer.stop()
        except Exception:
            pass
    win.closed.connect(_on_closed)

    win._live_timer = timer
    win._live_tick = tick
    timer.start()
    win.show()
    return stop, win


def start_live_acoustic_session(fanout,
                                channels,
                                FBGs,
                                settings: dict,
                                rep_rate_hz: float = 2000.0,
                                title_prefix: str = "Acoustic spectra",
                                on_error=None,
                                on_all_closed=None,
                                queue_maxsize: int = 200000):
    """
    Окна live-спектров акустических шумов: по одному на канал (подграфик на решётку).

    fanout   — уже запущенный FrameFanout (общий с «Plot live dynamics»).
    channels — 1-based каналы; FBGs — 1-based решётки на каждый канал.
    on_all_closed() вызывается, когда пользователь закрыл все окна крестиком.

    Возвращает (stop_all, info). stop_all отписывает очереди от fanout и
    «замораживает» окна (они остаются открытыми для изучения).
    """
    _require_app()
    stops, wins, queues = [], [], []
    closed_count = {"n": 0}

    try:
        for idx, ch in enumerate(channels):
            fbg1 = FBGs[idx] if idx < len(FBGs) else []
            if not fbg1:
                continue
            q = queue.Queue(maxsize=int(queue_maxsize))
            fanout.add_consumer_queue(q)
            queues.append(q)
            stop, win = live_plot_acoustic_spectra(
                channel=int(ch),
                fbg_indices=[int(x) - 1 for x in fbg1],
                source_queue=q,
                settings=settings,
                expected_rate_hz=float(rep_rate_hz),
                title=f"{title_prefix} (ch {ch})",
                on_error=on_error,
            )
            stops.append(stop)
            wins.append(win)
    except Exception:
        for q in queues:
            try:
                fanout.remove_consumer_queue(q)
            except Exception:
                pass
        for s in stops:
            try:
                s()
            except Exception:
                pass
        raise

    if not wins:
        raise ValueError("Нет включённых решёток: задайте каналы/FBG в Set interrogator parameters")

    done = {"stopped": False}

    def _one_closed():
        closed_count["n"] += 1
        if closed_count["n"] >= len(wins) and not done["stopped"] and on_all_closed:
            try:
                on_all_closed()
            except Exception:
                pass
    for w in wins:
        w.closed.connect(_one_closed)

    def stop_all():
        if done["stopped"]:
            return
        done["stopped"] = True
        for q in queues:
            try:
                fanout.remove_consumer_queue(q)
            except Exception:
                pass
        for s in stops:
            try:
                s()
            except Exception:
                pass

    return stop_all, {"windows": wins, "queues": queues, "stops": stops}
