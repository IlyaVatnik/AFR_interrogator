# -*- coding: utf-8 -*-
"""
live_plot_qt.py — интерактивные live-графики на PyQt5 + pyqtgraph (v3.0.0).

Заменяют прежние matplotlib-окна для кнопок «Plot live dynamics» / «Plot live
spectra», а также для live-графика при записи и при динамических измерениях.

Публичный API (его вызывает AFR_interrogator.FBGRecorder; сигнатуры сохранены):
    live_plot_wavelengths(...)      -> (stop, window)
    start_live_spectra_session(...) -> (stop_all, info)
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
