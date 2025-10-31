# -*- coding: utf-8 -*-
"""
Created on Mon Oct 27 20:38:04 2025

@author: User
"""

from AFR_interrogator.FBGRecorder import read_fbg_stream_raw_lp
import matplotlib.pyplot as plt
import numpy as np
file=r"C:\Users\Илья\fbg_dump.pkl"
# 3) прочитать файл
times, channels = read_fbg_stream_raw_lp(file)

print("samples:", times.size, "channels:", len(channels), "shape ch0:", channels[0].shape)
#%%
    

plt.figure()
plt.plot(times-times[0],channels[0][2])
plt.xlabel('Time, s')
plt.ylabel('FBG wavelength, nm')
acq_rate=1/np.mean(np.diff(times))
print(acq_rate, 'Hz')
plt.show()

#%%
