# -*- coding: utf-8 -*-
"""
Created on Mon Oct 27 20:26:04 2025

@author: User
"""

from AFR_interrogator.AFR_interrogator import Interrogator
from AFR_interrogator.FBGRecorder import record_to_file
import time
import numpy as np


it = Interrogator('10.2.60.37')
#%%
  # Сбор данных 5 секунд
ch=1
it.set_gain(ch, auto=False, manual_level=1)
it.set_threshold(ch, 3000)
time.sleep(0.1)
it.start_freq_stream()

# from afr_recorder import record_to_file, read_fbg_stream_raw_lp, safe_stop_interrogator

# 1) создать/запустить интеррогатор
# it = InterrogatorUDP(cfg)
# it.start_freq_stream()

# 2) записать 10 секунд в файл
stats = record_to_file(it, "fbg_dump.pkl", duration_sec=10.0,channels=[1],FBGs=[[1,2,3]],write_every_n=10)
print("Запись завершена:", stats)
it.stop_freq_stream()

