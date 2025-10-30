# -*- coding: utf-8 -*-
"""
Created on Mon Oct 27 20:53:39 2025

@author: User
"""
from AFR_interrogator.AFR_interrogator import Interrogator
from AFR_interrogator.FBGRecorder import live_plot_wavelengths
import time


it = Interrogator('10.2.60.37')
#%%
ch=1
it.set_gain(ch, auto=False, manual_level=1)
it.set_threshold(ch, 3000)
time.sleep(0.1)
it.start_freq_stream()

# # 4) live-плот (запускать из главного GUI-потока)
stop_live = live_plot_wavelengths(it, channel=1, fbg_indices=[0, 1, 2], window_sec=10.0, max_fps=100)

    #%%
# # ... когда нужно остановить:
stop_live()
it.stop_freq_stream()

#%%
