# -*- coding: utf-8 -*-
"""
Created on Fri Mar 20 13:52:48 2026

@author: Admin
"""

from AFR_interrogator.interrogator import Interrogator,Params_interrogator
from Printer_control.Printer import Printer,PrinterConfig
import time
time_max=60*60*3
time_sleep=20
time_meas=1

params=Params_interrogator()
params.FBGs=[[1,2]]
params.thresholds=[4000,3000,3000,3000]
file_path='FBGs.long_dynamics'
printer=Printer(PrinterConfig())
it=Interrogator('10.2.15.150','10.2.15.158',params=params)
time0=time.time()
time_current=0
try:
    while time_current<time_max:
        
        print(time_current)
        temp_bed=printer.get_bed_temperature()[0]
        temp_chamber=printer.get_chamber_temperature()[0]
        FBGs=it.get_averaged_single_FBG_measurement(time_meas)
        with open(file_path,'a') as f:
            f.write(str([time_current,temp_bed, temp_chamber,FBGs])+'\n')
        time.sleep(time_sleep)
        time_current=time.time()-time0
    del it
except (Exception, KeyboardInterrupt) as e:
    print(e)
    del it
   