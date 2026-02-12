# -*- coding: utf-8 -*-
"""
Created on Mon Feb  9 16:55:54 2026

@author: Ilya
"""

import pickle
import matplotlib.pyplot as plt
import numpy as np
import ast
from scipy.interpolate import griddata


file_name='static_measurements_data.txt'
channels_to_plot=[2]
FBGs_to_plot=[[],[1,2,3]]



coords=[]
temps_bed=[]
temps_chamber=[]
FBGs_map_pristine=[]
FBGs_map_pressed=[]
# Открываем файл для чтения

with open(file_name, 'r') as file:
    for line in file:
        # Убираем пробелы и символы новой строки
        line = line.strip()
        
        try:
            # Преобразование строки в список
            data = ast.literal_eval(line)

            # Извлечение переменных
            coords.append([data[0],data[1]])
            temps_bed.append(data[2])
            temps_chamber.append(data[3])
            FBGs_map_pristine.append(data[4])
            FBGs_map_pressed.append(data[5])

        except (ValueError, SyntaxError) as e:
            print(f"Ошибка при обработке строки: {line}. Ошибка: {e}")
coords=np.array(coords)

temps_bed=np.array(temps_bed)  
temps_chamber=np.array(temps_chamber)
#%%
def plot_3d(coords,Z):
    x, y = coords[:, 0], coords[:, 1]
    X, Y = np.meshgrid(x, y)
    nx, ny = len(x),len(y)
    xi = np.linspace(x.min(), x.max(), nx)
    yi = np.linspace(y.min(), y.max(), ny)
    X, Y = np.meshgrid(xi, yi)
    re_Z = griddata(coords, Z, (X, Y), method="cubic")  # можно: "linear", "nearest"

# === 2) Рисуем 3D поверхность + исходные точки ===
    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(111, projection="3d")

    surf = ax.plot_surface(X, Y, re_Z, cmap="inferno", linewidth=0, antialiased=True, alpha=0.9)
    # ax.scatter(x, y, Z, c="cyan", edgecolor="k", s=60)  # исходные измерения
    return fig,ax


def extract_FBG_wavelengths(FBGs_map,ch,FBG_number):
    FBG_wavelengths=[]
    try:
        for line in FBGs_map:
            FBG_wavelengths.append(line[ch-1][FBG_number-1])
        return np.array(FBG_wavelengths)
    except IndexError:
        print('there is no {} FBG in {} channel'.format(FBG_number, ch))
#%%

plot_3d(coords,temps_bed)
plt.xlabel('X, mm')
plt.ylabel('Y, mm')
plt.gca().set_zlabel("Bed temperature")
plt.tight_layout()

plot_3d(coords,temps_chamber)
plt.xlabel('X, mm')
plt.ylabel('Y, mm')
plt.gca().set_zlabel("Chamber temperature")
plt.tight_layout()


for ch in channels_to_plot:
    for FBG in FBGs_to_plot[ch-1]:
        FBG_wavelengths_pristine=extract_FBG_wavelengths(FBGs_map_pristine,ch,FBG)
        FBG_wavelengths_pressed=extract_FBG_wavelengths(FBGs_map_pressed,ch,FBG)
        fig,ax=plot_3d(coords,FBG_wavelengths_pressed-FBG_wavelengths_pristine)
        plt.xlabel('X, mm')
        plt.ylabel('Y, mm')
        ax.set_zlabel("FBG wavelength shift, nm")
        ax.set_title("ch={} FBG={}".format(ch,FBG))
        plt.tight_layout()
  
  
                
        




