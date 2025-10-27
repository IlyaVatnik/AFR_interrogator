# -*- coding: utf-8 -*-
"""
Created on Mon Oct 27 20:53:39 2025

@author: User
"""
from AFR_interrogator.AFR_interrogator import Interrogator
from AFR_interrogator.FBGRecorder import live_plot_wavelengths,safe_stop_interrogator
import time
import numpy as np


it = Interrogator()
#%%
ch=2
it.set_gain(ch, auto=True, manual_level=1)
it.set_threshold(ch, 2200)
time.sleep(0.1)
it.start_freq_stream()

# # 4) live-плот (запускать из главного GUI-потока)
stop_live = live_plot_wavelengths(it, channel=1, fbg_indices=[0, 1, 2], window_sec=4.0, max_fps=60)
# # ... когда нужно остановить:
    #%%
stop_live()
#
safe_stop_interrogator(it)
