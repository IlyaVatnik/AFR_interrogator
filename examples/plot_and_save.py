# -*- coding: utf-8 -*-
"""
Created on Mon Oct 27 21:08:48 2025

@author: User
"""

import matplotlib
matplotlib.use("Qt5Agg")  # или TkAgg
from AFR_interrogator.FBGRecorder import record_and_plot
from AFR_interrogator.AFR_interrogator import Interrogator
import time
import numpy as np


it = Interrogator()
#%%
# прибор уже создан
it.set_gain(2, auto=True, manual_level=1)
it.set_threshold(2, 2200)
it.start_freq_stream()

stop_all, stats = record_and_plot(
    it,
    filepath="simul_dump.pkl",
    duration_sec=10.0,
    batch_size=1000,
    warmup_sec=1.0,
    drop_during_warmup=True,
    plot_channel=2,
    plot_fbg_indices=[0,1,2],
    window_sec=10.0,
    max_fps=30
)
#%%
# ... окно живёт; когда захотите — останавливайте
stop_all()

# После закрытия окна или по таймеру — можно остановить:
# time.sleep(12); stop_all()