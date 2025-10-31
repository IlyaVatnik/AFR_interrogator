# -*- coding: utf-8 -*-
"""
Created on Mon Oct 27 21:08:48 2025

@author: User
"""

import matplotlib
matplotlib.use("TkAgg")  # или TkAgg
from AFR_interrogator.FBGRecorder import record_and_plot
from AFR_interrogator.AFR_interrogator import Interrogator



it = Interrogator('10.2.60.37')
#%%
# прибор уже создан
it.set_gain(1, auto=False, manual_level=1)
it.set_threshold(1, 3000)
it.start_freq_stream()

stop_all, stats = record_and_plot(
    it,
    channels=[1],
    FBGs=[[1,2,3]],
    write_every_n=10,
    filepath="fbg_dump.pkl",
    duration_sec=10.0,
    plot_channel=1,
    plot_fbg_indices=[0,1,2],
    window_sec=10.0,
    max_fps=30    
)
#%%
# ... окно живёт; когда захотите — останавливайте
stop_all()
it.stop_freq_stream()
# После закрытия окна или по таймеру — можно остановить:
# time.sleep(12); stop_all()
