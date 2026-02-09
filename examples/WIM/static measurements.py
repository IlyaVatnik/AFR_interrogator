# -*- coding: utf-8 -*-
"""
Created on Mon Feb  9 12:29:21 2026

@author: Ilya
"""


import numpy as np
from Printer_control.Printer import Printer, PrinterConfig
from AFR_interrogator.interrogator import Interrogator, average_FBG_measurements
import time

it = Interrogator('10.2.15.150','10.2.15.158')
p = Printer(PrinterConfig(
    base_url="http://10.2.15.109:7125",
    attach_min_x=-15,  attach_max_x=15,
    attach_min_y=-5,  attach_max_y=0,
    attach_min_z=-94, attach_max_z=0,
    ))

#%%
velocity_mm_s=100
accel_mm_s2=500
p.set_motion_limits(velocity_mm_s=velocity_mm_s, accel_mm_s2=accel_mm_s2)
p.set_bed_temperature(30)

z_safe=125
z_contact=110
averaging_time_for_single_FBG_measurement=1
file_name='static_measurements_data.txt'
X_array=np.arange(40,288,5)
Y_array=np.arange(135,175,3)
time_est=len(X_array)*len(Y_array)*(averaging_time_for_single_FBG_measurement+0.5)
print('estimated time={} min'.format(time_est/60))
#%%

p.home('XYZ')

#%%



for x in X_array:
    for y in Y_array:
        print(x,y)
        p.safe_y_pass(x=x, y_start=y, y_end=y, z_safe=z_safe, z_contact=z_safe)

        time.sleep(0.1)
        time0=time.time()
        FBGs_list=[]
        while time.time()-time0<averaging_time_for_single_FBG_measurement:
            FBGs_list.append(it.get_single_FBG_measurement())
        FBGs_pristine=average_FBG_measurements(FBGs_list)
        
        p.move_absolute(x=x, y=y, z=z_contact, speed_mm_s=velocity_mm_s)
        time.sleep(0.1)
        time0=time.time()
        FBGs_list=[]
        while time.time()-time0<averaging_time_for_single_FBG_measurement:
            FBGs_list.append(it.get_single_FBG_measurement())
        FBGs_pressured=average_FBG_measurements(FBGs_list)
        temp_bed=p.get_bed_temperature()[0]
        temp_chamber=p.get_chamber_temperature()[0]
        p.move_absolute(x=x, y=y, z=z_safe, speed_mm_s=velocity_mm_s)
        with open(file_name,'a') as f:
            f.write(str([int(x),int(y),temp_bed, temp_chamber,FBGs_pristine,FBGs_pressured])+'\n')
            
            
    
        