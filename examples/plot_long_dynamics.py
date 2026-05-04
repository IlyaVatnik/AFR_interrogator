# -*- coding: utf-8 -*-
"""
Created on Fri Mar 20 14:08:50 2026

@author: Admin
"""

import numpy as np
import matplotlib.pyplot as plt
import ast

file=r"D:\AFR_interrogator\examples\FBGs.long_dynamics"
def load_data(file_name):

    
    with open(file_name, 'r') as file:
        
        N_FBGs_list=None
        while N_FBGs_list==None:
            line = file.readline().strip()   # читаем только одну строку 
            data = ast.literal_eval(line)
            try: 
                N_FBGs_list=[len(x) for x in data[3]]
            except TypeError:
                continue
        times=[data[0]]
        temps_bed=[data[1]]
        temps_chamber=[data[2]]
        FBGs_map=[data[3]]
        
        
        for line in file:
            # Убираем пробелы и символы новой строки
            line = line.strip()
            
            try:
                data = ast.literal_eval(line)
                if [len(x) for x in data[3]]==N_FBGs_list: ## добавляет только те строки, где количество решеток равно начальному.
                    FBGs_map.append(data[3])
                    times.append(data[0])
                    temps_bed.append(data[1])
                    temps_chamber.append(data[2])
                # Преобразование строки в список
                   
    
                # Извлечение переменных
            except TypeError:
                pass

    
            except (ValueError, SyntaxError) as e:
                print(f"Ошибка при обработке строки: {line}. Ошибка: {e}")
    times=np.array(times)
    
    temps_bed=np.array(temps_bed)  
    temps_chamber=np.array(temps_chamber)
    # FBGs=np.array(FBGs_map)
    return times, temps_bed,temps_chamber,FBGs_map

def _extract_FBG_wavelengths(FBGs_map,ch,FBG_number):
    FBG_wavelengths=[]
    try:
        for line in FBGs_map:
            try:
                FBG_wavelengths.append(line[ch-1][FBG_number-1])
            except:
                FBG_wavelengths.append(np.nan)
        return np.array(FBG_wavelengths)
    except IndexError:
        print('there is no {} FBG in {} channel'.format(FBG_number, ch))
    except TypeError as e:
        print('Error while extracting {} FBG in {} channel:'.format(FBG_number, ch)+str(e))
        



times, temps_bed,temps_chamber,FBGs=load_data(file)
times/=60*60
fig,axes=plt.subplots(3,1,figsize=(12,8))
axes[0].plot(times, _extract_FBG_wavelengths(FBGs,1,0))
axes[0].set_ylabel('FBG 0 wavelength, nm')
axes[1].plot(times, _extract_FBG_wavelengths(FBGs,1,1))
axes[1].set_ylabel('FBG 1 wavelength, nm')
axes[2].plot(times, temps_bed,label='Bed temperature')
axes[2].plot(times, temps_chamber,label='Chamber temperature')
axes[2].set_ylabel('Temeperature, C')
plt.legend()
plt.xlabel('time, h')


        