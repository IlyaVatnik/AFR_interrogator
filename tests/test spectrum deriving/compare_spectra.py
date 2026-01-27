import numpy as np
import matplotlib.pyplot as plt
import pickle


def gain_value(gain: int) -> float:
    # Таблица соответствия 
    mapping = {
        5: 2.9059*1e-6,
        4: 4.356*1e-6,
       3: 6.4699*1e-6,
        2: 1.01289*1e-5,
        1: 1.50849*1e-5,
        0: 2.36161*1e-5
    }
    if gain not in mapping:
        raise ValueError(f"Unsupported gain")
    return mapping[gain]




with open('spectrum_from_our_app.spectrum','rb') as f:
    waves,spectrum_our=pickle.load(f)
        

plt.figure()
plt.plot(waves,spectrum_our,label='our')
plt.xlabel('Wavelength, nm')
plt.ylabel('Spectral power, dB')

#%%
data=np.genfromtxt('spectrum_from_manufacturer_app.txt',skip_header=1)
gain=0
waves_manufact,spectrum_manufact=299_792_458.0/data[:,0],10*np.log10(data[:,1])-48+10*gain_value(gain)
plt.plot(waves_manufact,spectrum_manufact,label='manufacturer')

plt.legend()

