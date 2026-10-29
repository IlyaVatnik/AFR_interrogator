# -*- coding: utf-8 -*-

__version__ = '2.0.0'
__date__ = '2026.10.29'

import os
import ast
import csv
import json
import pickle
import re
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Qt5Agg")
import matplotlib.pyplot as plt
from matplotlib import cm

from PyQt5.QtCore import QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QFileDialog, QDialog, QLineEdit, QComboBox, QCheckBox,
)

from FBGRecorder import (
    record_and_plot, record_to_file, read_fbg_stream_raw_lp,
    start_live_plot_session, record_spectra_to_file, read_spectra_from_file,
    start_live_spectra_session,
)
from interrogator import Interrogator, Params_interrogator
from interrogator_IP_config import AFRConfiguratorRS232, find_afr_interrogator_port
from long_term_measurements import (
    Long_term_measurement, Long_term_measurement_params,
)

from UIs.MainWindowUI import Ui_MainWindow


class Params_recording:
    def __init__(self):
        self.rep_rate = 2000
        self.recording_duration = 10
        self.write_every_nth = 10
        self.plot_live_while_recording = False
        self.type_of_recording = 'FBG peaks'
        self.file_name = '1'


class Params:
    def __init__(self):
        self.it = Params_interrogator()
        self.record = Params_recording()
        self.long_term = Long_term_measurement_params()


class ThreadedMainWindow(QMainWindow):

    force_long_term_process = pyqtSignal()

    def __init__(self, parent=None):
        QMainWindow.__init__(self, parent)
        self.threads = []
        self.destroyed.connect(self.kill_threads)

    def add_thread(self, objects):
        thread = QThread()
        self.threads.append(thread)
        for obj in objects:
            obj.moveToThread(thread)
        thread.start()
        return thread

    def kill_threads(self):
        for thread in self.threads:
            thread.quit()


class MainWindow(ThreadedMainWindow):

    _STYLE_GREEN = "QLabel { background-color: green; color: white; font-weight: bold; }"
    _STYLE_RED = "QLabel { background-color: red; color: white; font-weight: bold; }"

    def __init__(self, parent=None, version='0.0', date='0.0.0'):
        super().__init__(parent)

        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.init_menu_bar()

        self.ParametersFileName = 'Params.txt'
        self._base_dir = (
            Path(sys.executable).parent if getattr(sys, 'frozen', False)
            else Path(__file__).parent
        )
        self.path_to_main = str(self._base_dir)
        self.saving_dir_path = os.path.join(str(self._base_dir), 'data') + os.sep
        self.file_to_load_path = None
        self.setWindowTitle("AFR interrogator V." + version + ', released ' + date)

        self.params = Params()
        self.it = None

        self._active_fanout = None
        self._jump_timer = QTimer(self)
        self._jump_timer.setInterval(200)
        self._jump_timer.timeout.connect(self._poll_jump_count)
        self._prev_jump_count = 0

        self._loaded_kind = None
        self._loaded_times = None
        self._loaded_channels = None
        self._loaded_channel_list = []
        self._loaded_fbgs_list = []
        self._loaded_waves = None
        self._loaded_spectrum = None

        self.init_interface()
        self.load_parameters_from_file(self.ParametersFileName)

    def logText(self, text):
        self.ui.LogField.append(">" + str(text))

    def logWarningText(self, text):
        self.ui.LogField.append(
            "<span style=\" font-size:8pt; font-weight:600; color:#ff0000;\" >"
            + ">" + str(text) + "</span>"
        )

    def clear_log(self):
        self.ui.LogField.clear()

    def init_interface(self):
        self.ui.pushButton_choose_operation_folder.clicked.connect(self.choose_operation_folder)

        self.ui.pushButton_connect_it.toggled[bool].connect(self.connect_interrogator)
        self.ui.pushButton_set_it_parameters.pressed.connect(self.set_it_parameters)
        self.ui.pushButton_configure_interrogator_through_usb.pressed.connect(self.configure_it_through_usb)

        self.ui.pushButton_single_measurement.clicked.connect(self.single_measurement)
        self.ui.pushButton_save_single_spectrum.clicked.connect(self.save_single_spectrum)

        self.ui.pushButton_start_recording.pressed.connect(self.recording)
        self.ui.pushButton_plot_live_dynamics.toggled[bool].connect(self.plot_live_dynamics)
        self.ui.pushButton_plot_live_spectra.toggled[bool].connect(self.plot_live_spectra)
        self.ui.pushButton_set_recording_parameters.pressed.connect(self.set_recording_parameters)

        self.ui.pushButton_long_term_measurements.toggled[bool].connect(self.long_term_measurements)
        self.ui.pushButton_set_long_term_measurements_params.pressed.connect(
            self.set_long_term_measurements_params
        )

        self.ui.pushButton_choose_file_to_load.clicked.connect(self.choose_file_to_load)
        self.ui.pushButton_plot_from_file.clicked.connect(self.plot_from_file)
        self.ui.pushButton_save_single_line_to_csv.pressed.connect(self.save_single_line_to_csv)
        self.ui.comboBox_channel_to_plot_static_slice.currentIndexChanged.connect(
            lambda _: self._update_fbg_combo()
        )

        self.ui.pushButton_clearLog.clicked.connect(self.clear_log)

    def init_menu_bar(self):
        self.ui.action_save_parameters.triggered.connect(self.save_parameters_to_file)
        self.ui.action_load_parameters.triggered.connect(lambda: self.load_parameters_from_file())
        self.ui.action_delete_all_figures.triggered.connect(self.delete_all_figures)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_interrogator(self, pressed):
        if pressed:
            try:
                self.it = Interrogator(self.params.it.it_IP, self.params.it.PC_IP)
                set_parameters(self.it.params, get_parameters(self.params.it))
                time.sleep(0.1)
                self.set_gains()
                self.logText('Connected to interrogator')
            except Exception as e:
                self.logWarningText(str(e))
                self.it = None
                try:
                    self.ui.pushButton_connect_it.setChecked(False)
                except Exception:
                    pass
        else:
            try:
                if self.ui.pushButton_plot_live_dynamics.isChecked():
                    self.ui.pushButton_plot_live_dynamics.setChecked(False)
                if self.ui.pushButton_plot_live_spectra.isChecked():
                    self.ui.pushButton_plot_live_spectra.setChecked(False)
                if self.ui.pushButton_long_term_measurements.isChecked():
                    self.ui.pushButton_long_term_measurements.setChecked(False)
            except Exception:
                pass

            try:
                if self.it is not None:
                    self.it.stop_freq_stream()
            except Exception:
                pass

            self._set_active_fanout(None)
            self.it = None
            self.logText('disconnected from interrogator')

    def set_gains(self):
        if self.it is None:
            return
        self.it.set_gains()

    def configure_it_through_usb(self):
        try:
            port = find_afr_interrogator_port(verbose=True)
            if not port:
                self.logWarningText("AFR interrogator not found on any COM port")
                return
            it = AFRConfiguratorRS232(port)
            try:
                it.set_settings(self.params.it.it_IP, self.params.it.PC_IP)
                settings = it.query_settings()
            finally:
                try:
                    it.close()
                except Exception:
                    pass
            self.logText(str(settings))
        except Exception as e:
            self.logWarningText(str(e))

    def set_it_parameters(self):
        d = get_parameters(self.params.it)
        from UIs.it_parameters_dialogUI import Ui_Dialog
        it_parameters_dialog = QDialog()
        ui = Ui_Dialog()
        ui.setupUi(it_parameters_dialog)
        set_widget_values(it_parameters_dialog, d)

        channels = d.get('channels', [])
        FBGs = d.get('FBGs', [])
        for i, ch in enumerate(channels):
            if 1 <= ch <= 4:
                cb = getattr(ui, 'checkBox_ch_{}'.format(ch))
                le = getattr(ui, 'lineEdit_fbg_{}'.format(ch))
                cb.setChecked(True)
                if i < len(FBGs):
                    le.setText(', '.join(str(x) for x in FBGs[i]))

        if it_parameters_dialog.exec_() == QDialog.Accepted:
            params = get_widget_values(it_parameters_dialog)
            if 'rep_rate' in params:
                params['rep_rate'] = int(float(params['rep_rate']))

            new_channels = []
            new_FBGs = []
            for ch in range(1, 5):
                cb = getattr(ui, 'checkBox_ch_{}'.format(ch))
                if cb.isChecked():
                    new_channels.append(ch)
                    le = getattr(ui, 'lineEdit_fbg_{}'.format(ch))
                    text = le.text().strip()
                    try:
                        fbg_list = [
                            int(x) for x in text.replace(';', ',').split(',')
                            if x.strip() != ''
                        ]
                    except ValueError:
                        fbg_list = []
                    new_FBGs.append(fbg_list)

            params['channels'] = new_channels
            params['FBGs'] = new_FBGs
            set_parameters(self.params.it, params)

            if self.it is not None:
                set_parameters(self.it.params, params)
                self.it.set_gains()

    def set_recording_parameters(self):
        d = get_parameters(self.params.record)
        from UIs.recording_parameters_dialogUI import Ui_Dialog
        parameters_dialog = QDialog()
        ui = Ui_Dialog()
        ui.setupUi(parameters_dialog)
        set_widget_values(parameters_dialog, d)
        if parameters_dialog.exec_() == QDialog.Accepted:
            params = get_widget_values(parameters_dialog)
            set_parameters(self.params.record, params)

    def set_long_term_measurements_params(self):
        d = get_parameters(self.params.long_term)
        from UIs.long_term_recording_parameters_dialogUI import Ui_Dialog
        parameters_dialog = QDialog()
        ui = Ui_Dialog()
        ui.setupUi(parameters_dialog)
        set_widget_values(parameters_dialog, d)
        if parameters_dialog.exec_() == QDialog.Accepted:
            params = get_widget_values(parameters_dialog)
            set_parameters(self.params.long_term, params)

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def single_measurement(self):
        if self.it is None:
            self.logWarningText('Interrogator is not connected')
            return
        try:
            FBGs = self.it.get_averaged_single_FBG_measurement(
                self.params.it.averaging_time_for_single_FBG_measurement
            )
            if FBGs is None:
                self.logWarningText('error. no data returned from Interrogator')
                return

            string = ''
            for ch in self.params.it.channels:
                if ch - 1 < len(FBGs) and FBGs[ch - 1] is not None:
                    string += f'channel{ch}:  ' + (
                        ", ".join(f"{x:.3f}" for x in FBGs[ch - 1])
                    ) + ' nm'

            self.logText(string)

            if self.ui.checkBox_plot_single_spectrum.isChecked():
                waves = self.it.get_waves()
                for ch in self.params.it.channels:
                    spectrum = self.it.get_single_spectrum(ch)
                    plt.figure()
                    plt.plot(waves, spectrum)
                    plt.xlabel('Wavelength, nm')
                    plt.ylabel('Spectral power, dBm')
                    if ch - 1 < len(FBGs) and FBGs[ch - 1] is not None:
                        for FBG_wave in FBGs[ch - 1]:
                            if FBG_wave is not np.nan:
                                plt.axvline(FBG_wave, color='red')
                    plt.axhline(
                        self.it.get_log_threshold(ch), ls='--', color='gray', alpha=0.3
                    )
                    plt.title('Channel {}'.format(ch))
                plt.show(block=False)
        except Exception as e:
            self.logWarningText(str(e))

    def save_single_spectrum(self):
        try:
            line = plt.gca().get_lines()[0]
            waves = line.get_xdata()
            signal = line.get_ydata()
            self._ensure_saving_dir()
            filepath = os.path.join(
                self.saving_dir_path,
                self.ui.lineEdit_file_name_to_save_spectrum.text() + '.spectrum'
            )
            with open(filepath, 'wb') as f:
                pickle.dump([waves, signal], f)
            self.logText('Spectrum saved to ' + filepath)
        except Exception as e:
            self.logWarningText(str(e))

    def _set_active_fanout(self, fan):
        self._active_fanout = fan
        self._prev_jump_count = 0
        try:
            self.ui.label_FBG_lost.setStyleSheet(self._STYLE_GREEN)
        except Exception:
            pass
        if fan is not None:
            self._jump_timer.start()
        else:
            self._jump_timer.stop()

    def _poll_jump_count(self):
        fan = self._active_fanout
        if fan is None:
            self._jump_timer.stop()
            return
        jc = getattr(fan, 'jump_count', 0)
        if jc > self._prev_jump_count:
            self.ui.label_FBG_lost.setStyleSheet(self._STYLE_RED)
            self._prev_jump_count = jc

    def plot_live_dynamics(self, pressed: bool):
        if pressed:
            if self.it is None:
                self.logWarningText('Interrogator is not connected')
                self.ui.pushButton_plot_live_dynamics.setChecked(False)
                return
            try:
                self.it.start_freq_stream(self.params.it.rep_rate)

                self._stop_live, self._live_info = start_live_plot_session(
                    it=self.it,
                    plot_channels=list(self.params.it.channels),
                    plot_FBGs=list(self.params.it.FBGs),
                    rep_rate_hz=float(self.params.it.rep_rate),
                    window_sec=20.0,
                    max_fps=30,
                    ylim=None,
                    title_prefix="Live dynamics",
                    use_subplots=True,
                    queue_maxsize=10000,
                    max_frames_per_update=1200
                )

                self._set_active_fanout(self._live_info.get("fanout"))
                self.logText("Live dynamics started")

            except Exception as e:
                self.logWarningText(str(e))
                try:
                    self.ui.pushButton_plot_live_dynamics.setChecked(False)
                except Exception:
                    pass
        else:
            try:
                if hasattr(self, "_stop_live") and self._stop_live is not None:
                    try:
                        self._stop_live()
                    except Exception:
                        pass
                    self._stop_live = None
                    self._live_info = None

                try:
                    if self.it is not None:
                        self.it.stop_freq_stream()
                except Exception:
                    pass

                self._set_active_fanout(None)
                self.logText("Live dynamics stopped")

            except Exception as e:
                self.logWarningText(str(e))

    def plot_live_spectra(self, pressed: bool):
        if pressed:
            if self.it is None:
                self.logWarningText('Interrogator is not connected')
                self.ui.pushButton_plot_live_spectra.setChecked(False)
                return
            try:
                waves = self.it.get_waves()
                if waves is None:
                    self.it.update_waves()
                    waves = self.it.get_waves()

                self._stop_live_spectra, _ = start_live_spectra_session(
                    it=self.it,
                    channels=list(self.params.it.channels),
                    waves=waves,
                    on_error=self.logWarningText,
                    on_status=self.logText,
                )
                self.logText(
                    "Live spectra started ({} points, channels {})".format(
                        len(waves), list(self.params.it.channels)
                    )
                )
            except Exception as e:
                self.logWarningText(str(e))
                try:
                    self.ui.pushButton_plot_live_spectra.setChecked(False)
                except Exception:
                    pass
        else:
            try:
                if hasattr(self, "_stop_live_spectra") and self._stop_live_spectra is not None:
                    try:
                        self._stop_live_spectra()
                    except Exception:
                        pass
                    self._stop_live_spectra = None
                self.logText("Live spectra stopped")
            except Exception as e:
                self.logWarningText(str(e))

    def recording(self):
        if self.it is None:
            self.logWarningText('Interrogator is not connected')
            return

        self._ensure_saving_dir()
        self.logText('start recording')

        if self.params.record.type_of_recording == 'FBG peaks':
            filepath = self._saving_file_path(self.params.record.file_name, '.fbgs')
            if not self.params.record.plot_live_while_recording:
                try:
                    self.it.start_freq_stream(self.params.record.rep_rate)
                    stats = record_to_file(
                        self.it, filepath,
                        duration_sec=self.params.record.recording_duration,
                        channels=self.params.it.channels,
                        FBGs=self.params.it.FBGs,
                        write_every_n=self.params.record.write_every_nth
                    )
                    self.logText("Recording finished: {}".format(stats))
                    self.it.stop_freq_stream()
                except Exception as e:
                    self.logWarningText(str(e))
                try:
                    self.it.stop_freq_stream()
                except Exception:
                    pass
            else:
                try:
                    self.it.start_freq_stream()
                    self._stop_all, stats = record_and_plot(
                        self.it,
                        channels=self.params.it.channels,
                        FBGs=self.params.it.FBGs,
                        write_every_n=self.params.record.write_every_nth,
                        filepath=filepath,
                        duration_sec=self.params.record.recording_duration,
                        plot_channels=self.params.it.channels,
                        plot_FBGs=np.array(self.params.it.FBGs) - 1,
                        window_sec=10.0,
                        max_fps=30
                    )
                    QTimer.singleShot(
                        int(self.params.record.recording_duration * 1000),
                        self._stop_all
                    )
                    self.logText("Recording finished: {}".format(stats))
                except Exception as e:
                    self.logWarningText(str(e))

        elif self.params.record.type_of_recording == 'Spectra':
            filepath = self._saving_file_path(self.params.record.file_name, '.spectra')
            try:
                record_spectra_to_file(
                    self.it,
                    write_every_n=self.params.record.write_every_nth,
                    filepath=filepath,
                    duration_sec=self.params.record.recording_duration,
                    channels=self.params.it.channels
                )
                self.logText("Recording of spectra finished")
            except Exception as e:
                self.logWarningText(str(e))

    def long_term_measurements(self, pressed):
        if pressed:
            if self.it is None:
                self.logWarningText('Interrogator is not connected')
                self.ui.pushButton_long_term_measurements.setChecked(False)
                return
            file_name = str(self.params.long_term.file_name).strip()
            if not file_name:
                self.logWarningText('Long-term measurement file name is empty')
                self.ui.pushButton_long_term_measurements.setChecked(False)
                return
            try:
                self._ensure_saving_dir()
                self.long_term_measurement = Long_term_measurement(
                    self.it,
                    self.params.long_term,
                    self._saving_file_path(file_name, '.long_dynamics')
                )
                self.add_thread([self.long_term_measurement])

                self.long_term_measurement.S_print[str].connect(self.logText)
                self.long_term_measurement.S_print_error[str].connect(self.logWarningText)
                self.long_term_measurement.S_finished.connect(self._on_long_term_finished)

                self.force_long_term_process.connect(self.long_term_measurement.run)
                self.long_term_measurement.is_running = True
                self.logText('Start long-term measurement')
                self.force_long_term_process.emit()
            except Exception as e:
                self.logWarningText(str(e))
                try:
                    self.ui.pushButton_long_term_measurements.setChecked(False)
                except Exception:
                    pass
        else:
            try:
                self.long_term_measurement.is_running = False
            except Exception:
                pass

    def _on_long_term_finished(self):
        try:
            self.ui.pushButton_long_term_measurements.setChecked(False)
        except Exception:
            pass
        self.kill_long_term_measurement()

    def kill_long_term_measurement(self):
        try:
            del self.long_term_measurement
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Files and plots
    # ------------------------------------------------------------------

    def choose_operation_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Directory")
        if not folder:
            self.logWarningText('Folder is not chosen or previous choice is preserved')
            return
        self.saving_dir_path = folder.rstrip('\\/') + os.sep
        self.ui.label_operation_folder.setText(self.saving_dir_path)

    def choose_file_to_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Data File", '',
            'Data files (*.fbgs *.spectrum *.spectra *.long_dynamics);;All files (*)'
        )
        if not path:
            self.logWarningText('File is not chosen or previous choice is preserved')
            return
        self.file_to_load_path = path
        self.ui.label_file_to_load.setText(path)

    def plot_from_file(self):
        if not self.file_to_load_path:
            self.logWarningText('File is not chosen')
            return
        if not os.path.exists(self.file_to_load_path):
            self.logWarningText('File does not exist: ' + self.file_to_load_path)
            return

        ext = os.path.splitext(self.file_to_load_path)[1].lower()
        try:
            if ext == '.fbgs':
                self._plot_fbgs_file(self.file_to_load_path)
            elif ext == '.spectrum':
                self._plot_spectrum_file(self.file_to_load_path)
            elif ext == '.spectra':
                self._plot_spectra_file(self.file_to_load_path)
            elif ext == '.long_dynamics':
                self._plot_long_dynamics_file(self.file_to_load_path)
            else:
                self.logWarningText('Unsupported file type: ' + ext)
        except Exception:
            self.logWarningText(traceback.format_exc())

    def _plot_fbgs_file(self, path):
        times, channels, channel_list, FBGs_list, other_params = read_fbg_stream_raw_lp(path)
        if len(times) == 0:
            self.logWarningText('No data found in file')
            return
        self.logText(
            'In this file there are channels {} and FBGs {} in these channels'.format(
                channel_list, FBGs_list
            )
        )
        self._loaded_kind = 'fbgs'
        self._loaded_times = times
        self._loaded_channels = channels
        self._loaded_channel_list = list(channel_list)
        self._loaded_fbgs_list = [list(x) for x in FBGs_list]
        self._update_channel_combo()
        self._plot_channels_data(times, channels, channel_list, FBGs_list)
        self.logText('Other parameters of the record are {} '.format(other_params))

    def _plot_spectrum_file(self, path):
        with open(path, 'rb') as f:
            waves, spectrum = pickle.load(f)
        self._loaded_kind = 'spectrum'
        self._loaded_waves = np.asarray(waves)
        self._loaded_spectrum = np.asarray(spectrum)
        plt.figure()
        plt.plot(waves, spectrum)
        plt.xlabel('Wavelength, nm')
        plt.ylabel('Spectral power, dBm')
        plt.tight_layout()
        plt.show(block=False)

    def _plot_spectra_file(self, path):
        n_ch = self._spectra_n_channels(path)
        self._loaded_kind = 'spectra'
        for ch in range(1, n_ch + 1):
            times, waves, spectra, other_params = read_spectra_from_file(path, ch)
            if len(times) > 1:
                self.logText('Average acquisition rate is {} Hz '.format(
                    1 / np.mean(np.diff(times))
                ))
            self.logText('Other parameters of the record are {} '.format(other_params))
            fig, ax = plt.subplots(subplot_kw={"projection": "3d"})
            X, Y = np.meshgrid(waves, times)
            ax.plot_surface(X, Y, spectra.T, cmap=cm.coolwarm, linewidth=0, antialiased=False)
            plt.ylabel('Time, s')
            plt.xlabel('Wavelength, nm')
            plt.gca().set_zlabel('Spectral power, dBm')
            plt.title('Channel {}'.format(ch))
            plt.tight_layout()
        plt.show(block=False)

    def _plot_long_dynamics_file(self, path):
        times, fbgs_map, fbg_counts = read_long_dynamics_text(path)
        if len(times) == 0:
            try:
                times, channels, channel_list, FBGs_list, other_params = (
                    read_fbg_stream_raw_lp(path)
                )
                if len(times) > 0:
                    self._loaded_kind = 'long_dynamics'
                    self._loaded_times = times
                    self._loaded_channels = channels
                    self._loaded_channel_list = list(channel_list)
                    self._loaded_fbgs_list = [list(x) for x in FBGs_list]
                    self._update_channel_combo()
                    self._plot_channels_data(times, channels, channel_list, FBGs_list)
                    self.logText(
                        'Long-term data loaded: {} points'.format(len(times))
                    )
                    return
            except Exception:
                pass
            self.logWarningText('No data found in file')
            return

        channels = {}
        channel_list = [
            ch for ch in range(1, len(fbg_counts) + 1) if fbg_counts[ch - 1] > 0
        ]
        FBGs_list = []
        for ch in channel_list:
            counts = fbg_counts[ch - 1]
            FBGs_list.append(list(range(1, counts + 1)))
            channels[ch] = {}
            for i in range(counts):
                channels[ch][i + 1] = np.array(
                    [_as_float(row[ch - 1][i]) if i < len(row[ch - 1]) else np.nan
                     for row in fbgs_map]
                )

        self._loaded_kind = 'long_dynamics'
        self._loaded_times = times
        self._loaded_channels = channels
        self._loaded_channel_list = channel_list
        self._loaded_fbgs_list = FBGs_list
        self._update_channel_combo()
        self._plot_channels_data(times, channels, channel_list, FBGs_list)
        self.logText('Long-term data loaded: {} points'.format(len(times)))

    def _plot_channels_data(self, times, channels, channel_list, FBGs_list):
        colors = plt.cm.tab10.colors
        for idx, ch in enumerate(channel_list):
            fbgs = list(FBGs_list[idx]) if idx < len(FBGs_list) else []
            if not fbgs:
                fbgs = list(channels.get(ch, {}).keys())
            if not fbgs:
                continue

            n_fbg = len(fbgs)
            if n_fbg > 1:
                fig, axes = plt.subplots(nrows=n_fbg, sharex=True)
                fig.supxlabel("Time, s")
                fig.supylabel("FBG wavelength, nm")
                for ii, fbg in enumerate(fbgs):
                    if fbg in channels.get(ch, {}):
                        axes[ii].plot(
                            times, channels[ch][fbg],
                            color=colors[ii % len(colors)]
                        )
                    axes[ii].set_title(f"FBG {fbg}", loc="left", fontsize=10, pad=2)
            else:
                fig, axes = plt.subplots()
                fig.supxlabel("Time, s")
                fig.supylabel("FBG wavelength, nm")
                fbg = fbgs[0]
                if fbg in channels.get(ch, {}):
                    axes.plot(times, channels[ch][fbg], color=colors[0])
                axes.set_title(f"FBG {fbg}", loc="left", fontsize=10, pad=2)

            fig.suptitle('ch {}'.format(ch))
            fig.tight_layout()
        plt.show(block=False)

    def _spectra_n_channels(self, path):
        meta_path = Path(str(path) + '.meta')
        if meta_path.exists():
            try:
                with open(meta_path, 'rb') as f:
                    meta = pickle.load(f)
                return int(meta.get('n_ch', len(self.params.it.channels)))
            except Exception:
                pass
        return len(self.params.it.channels)

    def _update_channel_combo(self):
        if not self._loaded_channel_list:
            return
        combo = self.ui.comboBox_channel_to_plot_static_slice
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([str(ch) for ch in self._loaded_channel_list])
        combo.blockSignals(False)
        self._update_fbg_combo()

    def _update_fbg_combo(self):
        if not self._loaded_channel_list or not self._loaded_fbgs_list:
            return
        try:
            ch = int(self.ui.comboBox_channel_to_plot_static_slice.currentText())
        except ValueError:
            return
        if ch not in self._loaded_channel_list:
            return
        idx = self._loaded_channel_list.index(ch)
        fbgs = self._loaded_fbgs_list[idx]
        combo = self.ui.comboBox_FBG_to_plot_static_slice
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([str(x) for x in fbgs])
        combo.blockSignals(False)

    def save_single_line_to_csv(self):
        if not self.file_to_load_path:
            self.logWarningText('File is not chosen')
            return
        try:
            source = os.path.splitext(os.path.basename(self.file_to_load_path))[0]
            folder = os.path.dirname(self.file_to_load_path)

            if self._loaded_kind in ('fbgs', 'long_dynamics'):
                ch = int(self.ui.comboBox_channel_to_plot_static_slice.currentText())
                fbg = int(self.ui.comboBox_FBG_to_plot_static_slice.currentText())
                if ch not in self._loaded_channels or fbg not in self._loaded_channels[ch]:
                    self.logWarningText('Selected channel/FBG not found in loaded data')
                    return
                x = self._loaded_times
                y = self._loaded_channels[ch][fbg]
                file_name = '{} ch={} FBG={}.csv'.format(source, ch, fbg)
                csv_line_saver(
                    os.path.join(folder, file_name),
                    x, y, 'Time, s', 'Wavelength, nm'
                )
            elif self._loaded_kind == 'spectrum':
                file_name = source + '.csv'
                csv_line_saver(
                    os.path.join(folder, file_name),
                    self._loaded_waves, self._loaded_spectrum,
                    'Wavelength, nm', 'Spectral power, dBm'
                )
            else:
                self.logWarningText(
                    'CSV export is available for .fbgs, .long_dynamics and .spectrum files'
                )
                return

            self.logText('Line saved to ' + os.path.join(folder, file_name))
        except Exception as e:
            self.logWarningText(str(e))

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def save_parameters_to_file(self):
        D = {}
        D['it'] = get_parameters(self.params.it)
        D['recording'] = get_parameters(self.params.record)
        D['long_term'] = get_parameters(self.params.long_term)
        D['main_window'] = get_widget_values(self)

        for k in D:
            l = [key for key in list(D[k].keys()) if ('path' in key)]
            for key in l:
                del D[k][key]

        default_path = os.path.join(self.path_to_main, self.ParametersFileName)
        SaveFilePath, _ = QFileDialog.getSaveFileName(
            self, "Save parameter file", default_path,
            'Parameters (*.parameters *.txt);;All files (*)'
        )
        if not SaveFilePath:
            self.logWarningText('File is not chosen or previous choice is preserved')
            return

        self.ParametersFileName = SaveFilePath
        f = open(self.ParametersFileName, 'w')
        json.encoder.FLOAT_REPR = lambda x: format(x, '.5f') if (x < 0.01) else x
        json.dump(D, f)
        f.close()
        self.logText('\nParameters saved\n')

    def load_parameters_from_file(self, file_path=None):
        if file_path is None:
            DataFilePath, _ = QFileDialog.getOpenFileName(
                self, "Select Parameter File", str(self._base_dir),
                'Parameters (*.parameters *.txt);;All files (*)'
            )
            if not DataFilePath:
                self.logWarningText('File is not chosen or previous choice is preserved')
                return
            self.ParametersFileName = DataFilePath
        else:
            DataFilePath = file_path

        try:
            data_path = Path(DataFilePath)
            if not data_path.is_absolute():
                data_path = self._base_dir / data_path

            with open(data_path) as f:
                Dicts = json.load(f)

            if Dicts is None:
                return

            if 'it' in Dicts:
                set_parameters(self.params.it, Dicts['it'])
                if self.it is not None:
                    set_parameters(self.it.params, Dicts['it'])
            if 'recording' in Dicts:
                set_parameters(self.params.record, Dicts['recording'])
            if 'long_term' in Dicts:
                set_parameters(self.params.long_term, Dicts['long_term'])
            if 'main_window' in Dicts:
                set_widget_values(self, Dicts['main_window'])

            self.logText('\nParameters loaded\n')

        except FileNotFoundError:
            self.logWarningText('Error while load parameters: Parameters file not found')
        except json.JSONDecodeError:
            self.logWarningText('Error while load parameters: file has wrong format')
        except Exception as e:
            self.logWarningText(str(e))

    def delete_all_figures(self):
        if plt.get_backend() != 'TkAgg':
            for i in plt.get_fignums():
                plt.close(i)
        else:
            self.logWarningText('Deleting figures does not work with TKinter backend')

        try:
            from live_plot_qt import close_all_live_windows
            close_all_live_windows()
        except Exception as e:
            self.logWarningText(f"Closing live plots error: {e}")

    def _ensure_saving_dir(self):
        try:
            os.makedirs(self.saving_dir_path, exist_ok=True)
        except Exception:
            pass

    def _saving_file_path(self, name, ext):
        name = str(name).strip()
        if not name:
            name = '1'
        return os.path.join(self.saving_dir_path, name + ext)

    def __del__(self):
        try:
            if self.it is not None:
                self.it._sock.shutdown(2)
                self.it._sock.close()
                self.it = None
        except Exception:
            pass


def _as_float(value):
    if value is None:
        return np.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def read_long_dynamics_text(file_path):
    times = []
    fbgs_map = []
    fbg_counts = None

    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            line = re.sub(r'\bnan\b', 'None', line)
            try:
                data = ast.literal_eval(line)
            except (ValueError, SyntaxError):
                continue
            if not isinstance(data, (list, tuple)) or len(data) < 2:
                continue

            row = None
            for idx in (1, 3):
                if idx < len(data) and isinstance(data[idx], (list, tuple)):
                    row = data[idx]
                    break
            if row is None:
                continue

            try:
                counts = [len(x) for x in row]
            except TypeError:
                continue

            if fbg_counts is None:
                fbg_counts = counts
            if counts != fbg_counts:
                continue

            try:
                times.append(float(data[0]))
            except (TypeError, ValueError):
                continue
            fbgs_map.append(row)

    if fbg_counts is None:
        fbg_counts = []

    return np.asarray(times, dtype=float), fbgs_map, fbg_counts


def get_widget_values(window):
    D = {}
    for w in window.findChildren(QLineEdit):
        s = w.text()
        key = w.objectName().split('lineEdit_')[1]
        try:
            f = int(s)
        except ValueError:
            try:
                f = float(s)
            except ValueError:
                f = s
        D[key] = f

    for w in window.findChildren(QCheckBox):
        f = w.isChecked()
        key = w.objectName().split('checkBox_')[1]
        D[key] = f

    for w in window.findChildren(QComboBox):
        s = w.currentText()
        key = w.objectName().split('comboBox_')[1]
        D[key] = s

    return D


def set_widget_values(window, d):
    for w in window.findChildren(QLineEdit):
        key = w.objectName().split('lineEdit_')[1]
        try:
            s = d[key]
            w.setText(str(s))
        except KeyError:
            pass

    for w in window.findChildren(QCheckBox):
        key = w.objectName().split('checkBox_')[1]
        try:
            s = d[key]
            w.setChecked(bool(s))
            w.clicked.emit(bool(s))
        except KeyError:
            pass

    for w in window.findChildren(QComboBox):
        key = w.objectName().split('comboBox_')[1]
        try:
            s = d[key]
            w.setCurrentText(str(s))
        except KeyError:
            pass


def set_parameters(obj, d):
    for key in d:
        if not hasattr(obj, key):
            continue
        try:
            if isinstance(d[key], str) and '[' in d[key]:
                setattr(obj, key, json.loads(d[key]))
            else:
                setattr(obj, key, d[key])
        except (TypeError, json.JSONDecodeError):
            pass


def get_parameters(obj):
    return dict(vars(obj)).copy()


def csv_line_saver(file_path, x, y, x_label, y_label):
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([x_label, y_label])
        writer.writerows(zip(x, y))
