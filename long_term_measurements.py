# -*- coding: utf-8 -*-

import math
import time

from PyQt5.QtCore import QObject, pyqtSignal


def _to_python(value):
    if isinstance(value, (list, tuple)):
        return [_to_python(x) for x in value]
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(f):
        return None
    return f


class Long_term_measurement_params:
    def __init__(self):
        self.sleep_time = 10
        self.duration = 100
        self.file_name = '1'


class Long_term_measurement(QObject):

    S_finished = pyqtSignal()
    S_print = pyqtSignal(str)
    S_print_error = pyqtSignal(str)

    def __init__(self, it, params, file_path):
        super().__init__()
        self.it = it
        self.params = params
        self.is_running = False
        self.file_path = file_path

    def run(self):
        time0 = time.time()
        time_current = 0.0
        duration = float(self.params.duration)
        sleep_time = float(self.params.sleep_time)

        try:
            while time_current < duration:
                if not self.is_running:
                    self.S_print_error.emit('Long term measurement interrupted')
                    break

                self.S_print.emit(
                    'Time = {:.1f} min of {:.1f} min'.format(
                        time_current / 60, duration / 60
                    )
                )

                FBGs = self.it.get_averaged_single_FBG_measurement()

                with open(self.file_path, 'a', encoding='utf-8') as f:
                    f.write(str([time_current, _to_python(FBGs)]) + '\n')

                time.sleep(sleep_time)
                time_current = time.time() - time0

            if self.is_running:
                self.S_print.emit('Long term measurement finished')
                self.is_running = False

        except Exception as e:
            self.S_print_error.emit('Error while long term measurement: ' + str(e))
        finally:
            self.is_running = False
            self.S_finished.emit()
