# -*- coding: utf-8 -*-
"""
Created on Wed Jan 21 11:32:18 2026

@author: Илья
"""

__version__='1.4.0'
__date__ = '2026.02.27'

import os
    
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import cm
import json

import pickle

from PyQt5.QtCore import  QThread,QTimer
from PyQt5.QtWidgets import QMainWindow, QFileDialog, QDialog,QLineEdit,QComboBox,QCheckBox,QMessageBox
import time



from FBGRecorder import record_and_plot,record_to_file,read_fbg_stream_raw_lp,live_plot_wavelengths,record_spectra_to_file,read_spectra_from_file
from interrogator import Interrogator, average_FBG_measurements



from UIs.MainWindowUI import Ui_MainWindow


class Params_it():
    def __init__(self):
        self.it_IP='10.2.60.38'
        self.PC_IP='10.2.60.33'
        self.FBGs=[[1,2,3]]
        self.channels=[1]
        self.gains_auto=[0,0,0,0]
        self.gains_manual=[1,1,1,1]
        self.thresholds=[3000,3000,3000,3000]
        self.averaging_time_for_single_FBG_measurement=0.5
        
class Params_recording():
    def __init__(self):      
        self.rep_rate=2000
        self.recording_duration=10
        self.write_every_nth=10
        self.plot_live_while_recording=False
        self.type_of_recording='FBG peaks'
    

class Params():
    def __init__(self):
        self.it=Params_it()
        self.record=Params_recording()
        


class ThreadedMainWindow(QMainWindow):

    def __init__(self, parent=None):
        QMainWindow.__init__(self, parent)

        # Handle threads
        self.threads = []
        self.destroyed.connect(self.kill_threads)

    
    def add_thread(self, objects):
        """
        Creates thread, adds it into to-destroy list and moves objects to it.
        Thread is QThread there.
        :param objects -- list of QObjects.
        :return None
        """
        # Create new thread
        thread = QThread()

        # Add new thread to list of threads to close on app destroy.
        self.threads.append(thread)

        # Move objects to new thread.
        for obj in objects:
            obj.moveToThread(thread)

        thread.start()
        return thread

    def kill_threads(self):
        """
        Closes all of created threads for this window.
        :return: None
        """
        # Iterate over all the threads and call wait() and quit().
        for thread in self.threads:
#            thread.wait()
            thread.quit()



class MainWindow(ThreadedMainWindow):
  
    
    '''
    Initialization
    '''
    def __init__(self, parent=None,version='0.0',date='0.0.0'):
        super().__init__(parent)
        self.path_to_main=os.getcwd()
        # GUI
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.init_menu_bar()
        self.init_interface()
        
        self.saving_dir_path='data\\'
        self.file_to_load_path=None
        
        
        self.ParametersFileName='Params.txt'
        self.setWindowTitle("AFR interrogator recorder V."+version+', released '+date)
        
        # self.it
        
        self.params=Params()
        self.load_parameters_from_file()
        
        self.it=None

        
  
        
    def logText(self, text):
        self.ui.LogField.append(">" + text)
        
    def logWarningText(self, text):
        self.ui.LogField.append("<span style=\" font-size:8pt; font-weight:600; color:#ff0000;\" >"
                             + ">" + text + "</span>")
        
    def clear_log(self):
        """Функция, которая вызывается по нажатию кнопки и очищает LogField."""
        self.ui.LogField.clear()
        
    def init_interface(self):
        self.ui.pushButton_start_recording.pressed.connect(self.recording)
        self.ui.pushButton_plot_live_dynamics.toggled[bool].connect(self.plot_live_dynamics)
        self.ui.pushButton_set_it_parameters.pressed.connect(self.set_it_parameters)
        self.ui.pushButton_set_recording_parameters.pressed.connect(self.set_recording_parameters)
        self.ui.pushButton_connect.toggled[bool].connect(self.connect_interrogator)
        self.ui.pushButton_choose_file_to_load.clicked.connect(self.choose_file_to_load)
        self.ui.pushButton_choose_folder_to_save.clicked.connect(self.choose_folder_to_save)
        self.ui.pushButton_plot_from_file.clicked.connect(self.plot_from_file)
        self.ui.pushButton_single_measurement.clicked.connect(self.single_measurement)
        self.ui.pushButton_save_single_spectrum.clicked.connect(self.save_single_spectrum)
        self.ui.pushButton_clearLog.clicked.connect(self.clear_log)
        
        
        
        
      
    def init_menu_bar(self):
        self.ui.action_save_parameters.triggered.connect(self.save_parameters_to_file)
        self.ui.action_load_parameters.triggered.connect(self.load_parameters_from_file)
        self.ui.action_delete_all_figures.triggered.connect(self.delete_all_figures)      
        
        
    
        
    def connect_interrogator(self,pressed):
        if pressed:
            try:
                self.it = Interrogator(self.params.it.it_IP,self.params.it.PC_IP)
                self.set_gains()
                # self.add_thread(self.it)
                self.logText('Connected to interrogator')
            except Exception as e:
                self.logWarningText(str(e))
        else:
            del self.it
            self.logText('disconnected from interrogator')
            
            
                  
    def set_it_parameters(self):
        '''
        open dialog with analyzer parameters
        '''
        d = get_parameters(self.params.it)
        from UIs.it_parameters_dialogUI import Ui_Dialog
        it_parameters_dialog = QDialog()
        ui = Ui_Dialog()
        ui.setupUi(it_parameters_dialog)
        set_widget_values(it_parameters_dialog,d)
        if it_parameters_dialog.exec_() == QDialog.Accepted:
            params=get_widget_values(it_parameters_dialog)
            set_parameters(self.params.it,params)
            if self.it!=None:
                self.set_gains()
            
    def set_gains(self):
        for ch in range(self.it.channels):
            self.it.set_gain(ch+1, auto=self.params.it.gains_auto[ch], manual_level=self.params.it.gains_manual[ch])
            self.it.set_threshold(ch+1, self.params.it.thresholds[ch])

    def single_measurement(self):
        try:
            FBGs=self.it.get_averaged_single_FBG_measurement(self.params.it.averaging_time_for_single_FBG_measurement)
            if FBGs==None:
                self.logWarningText('error. no data returned from Interrogator')
                return 
            string=''
            for ch in self.params.it.channels:

                    if FBGs[ch-1] is not None:
                        string+=f'channel{ch}:  '+(", ".join(f"{x:.3f}" for x in FBGs[ch-1]))+ ' nm'
    
            self.logText(string)
            if self.ui.checkBox_plot_single_spectrum.isChecked():
                waves=self.it.get_waves()
                for ch in self.params.it.channels:
                    spectrum=self.it.get_single_spectrum(ch)
                    plt.figure()
                    plt.plot(waves,spectrum)
                    plt.xlabel('Wavelength, nm')
                    plt.ylabel('Spectral power, dBm')
                    ymin, ymax = plt.ylim()
                    if FBGs[ch-1] is not None:
                        for FBG_wave in FBGs[ch-1]:
                            if FBG_wave is not np.nan:
                                plt.axvline(FBG_wave,  color='red')
                    plt.axhline(self.it.get_log_threshold(ch),ls='--',color='gray',alpha=0.3)
                    plt.title('Channel {}'.format(ch))
        except Exception as e:
            self.logWarningText(str(e))

    def set_recording_parameters(self):
        '''
        open dialog with analyzer parameters
        '''
        d = get_parameters(self.params.record)
        from UIs.recording_parameters_dialogUI import Ui_Dialog
        parameters_dialog = QDialog()
        ui = Ui_Dialog()
        ui.setupUi(parameters_dialog)
        set_widget_values(parameters_dialog,d)
        if parameters_dialog.exec_() == QDialog.Accepted:
            params=get_widget_values(parameters_dialog)
            set_parameters(self.params.record,params)
            if self.it!=None:
                self.set_gains()
                
    def plot_live_dynamics(self,pressed:bool):
        if pressed:    
            self.it.start_freq_stream()
            self.live_plots=[]
            for ch in self.params.it.channels:
                self.live_plots.append(live_plot_wavelengths(self.it, 
                                                             channel=ch, 
                                                             fbg_indices=np.array(self.params.it.FBGs[ch-1])-1, 
                                                             window_sec=10.0,
                                                             max_fps=30))
            # self.add_thread(self.stop_live())
        else:
            # self.kill_threads(self.stop_live)
            del self.live_plots
            # self.stop_live()
            self.it.stop_freq_stream()
            
            
    def recording(self):
        FilePrefix=self.ui.lineEdit_file_name.text()
        self.logText('start recording')
        
        if self.params.type_of_recording=='FBG peaks':
            if not self.params.record.plot_live_while_recording:
                
                try:
                    self.it.start_freq_stream(self.params.record.rep_rate)
                    stats = record_to_file(self.it, self.saving_dir_path+FilePrefix+".fbgs", duration_sec=self.params.record.recording_duration,
                                           channels=self.params.it.channels,FBGs=self.params.it.FBGs,write_every_n=self.params.record.write_every_nth)
                    self.logText("Recording finished: {}".format(stats))
                    self.it.stop_freq_stream()
                except Exception as e:
                    self.logWarningText(str(e))
                    
                self.it.stop_freq_stream()
                
            else:
                try:
                    self.it.start_freq_stream()
        
                    self._stop_all, stats = record_and_plot(
                        self.it,
                        channels=self.params.it.channels,
                        FBGs=self.params.it.FBGs,
                        write_every_n=self.params.record.write_every_nth,
                        filepath=self.saving_dir_path+FilePrefix+".fbgs",
                        duration_sec=self.params.record.recording_duration,
                        plot_channels=self.params.it.channels,
                        plot_FBGs=np.array(self.params.it.FBGs)-1,
                        window_sec=10.0,
                        max_fps=30    
                    )
                    QTimer.singleShot(int(self.params.record.recording_duration * 1000), self._stop_all)
                    self.logText("Recording finished: {}".format(stats))
                except Exception as e:
                    self.logWarningText(str(e))
                # ... окно живёт; когда захотите — останавливайте
            # stop_all()
            # self.it.stop_freq_stream()
        elif self.params.type_of_recording=='Spectra':
            record_spectra_to_file(self.it,
                                   write_every_n=self.params.record.write_every_nth,
                                   filepath=self.saving_dir_path+FilePrefix+".spectra",
                                   duration_sec=self.params.record.recording_duration,
                                   channels=self.params.it.channels
                                   )
        
        
    def choose_folder_to_save(self):
        self.saving_dir_path = str(
            QFileDialog.getExistingDirectory(self, "Select Directory"))+'\\'
        self.ui.label_folder_to_save.setText(self.saving_dir_path+'\\')
        
    def save_single_spectrum(self):
        line = plt.gca().get_lines()[0]
        waves = line.get_xdata()
        signal = line.get_ydata()
        # print(waves,signal)
        # wave_min, wave_max = plt.gca().get_xlim()
        # index_min = np.argmin(abs(waves-wave_min))
        # index_max = np.argmin(abs(waves-wave_max))
        # signal = signal[index_min:index_max]
        # waves = waves[index_min:index_max]
        # print(waves,signal)
        with open(self.saving_dir_path+'\\'+ self.ui.lineEdit_file_name_to_save_spectrum.text()+'.spectrum', "wb") as f:
            pickle.dump([waves,signal], f)
        self.logText('\nSpectrum saved\n')     
        
    def choose_file_to_load(self):
        DataFilePath= str(QFileDialog.getOpenFileName(self, "Select Data File",'','*.fbgs *.spectrum *.spectra' )).split("\',")[0].split("('")[1]
        if DataFilePath=='':
            self.logWarningText('file is not chosen or previous choice is preserved')
        self.file_to_load_path=DataFilePath
        self.ui.label_file_to_load.setText(DataFilePath)
 
        
    def plot_from_file(self):
        file_name=os.path.basename(self.file_to_load_path)
        if file_name.split('.')[1]=='fbgs':
            colors = plt.cm.tab10.colors
            times, channels, channel_list, FBGs_list,other_params = read_fbg_stream_raw_lp(self.file_to_load_path)
            self.logText('In this file there are channels {} and FBGs {} in these channels'.format(channel_list,FBGs_list))
            for ch in self.params.it.channels:
                N_FBG=len(self.params.it.FBGs[ch-1])
                fig,axes=plt.subplots(nrows=N_FBG,sharex=True)
                fig.supxlabel("Time, s")
                fig.supylabel("FBG wavelength, nm")
                for ii,FBG in enumerate(self.params.it.FBGs[ch-1]):
                    axes[ii].plot(times - times[0], channels[ch][ii+1],color=colors[ii % len(colors)])
                    axes[ii].set_title(f"FBG {FBG}", loc="left", fontsize=10, pad=2)
                plt.suptitle('ch {}'.format(ch))
                plt.tight_layout()
                plt.show()
                
            self.logText('Other parameters of the record are {} '.format(other_params))       
            
        elif file_name.split('.')[1]=='spectrum':
            with open(self.file_to_load_path,'rb') as f:
                waves,spectrum=pickle.load(f)
            plt.figure()
            plt.plot(waves,spectrum)
            plt.xlabel('Wavelength, nm')
            plt.ylabel('Spectral power, dBm')
            plt.tight_layout()
            
        elif file_name.split('.')[1]=='spectra':
            for ch in self.params.it.channels:
                times,waves,spectra=read_spectra_from_file(self.file_to_load_path,ch)
                fig, ax = plt.subplots(subplot_kw={"projection": "3d"})
                ax.plot_surface(times,waves, spectra, cmap=cm.coolwarm, linewidth=0, antialiased=False)
                plt.xlabel('Time, s')
                plt.ylabel('Wavelength, nm')
                plt.zlabel('Spectral power, dBm')
                plt.tight_layout()
        
          
    def save_parameters_to_file(self):
        '''
        save all parameters and values except paths to file

        Returns
        -------
        None.

        '''
        D={}
        D['it']=get_parameters(self.params.it)
        D['recording']=get_parameters(self.params.record)
        D['main_window']=get_widget_values(self)
        
        

        #remove all parameters that are absolute paths 
        for k in D:
            l=[key for key in list(D[k].keys()) if ('path' in key)]
            for key in l:
                del D[k][key]
    
        f=open(self.ParametersFileName,'w')
        json.encoder.FLOAT_REPR = lambda x: format(x, '.5f') if (x<0.01) else x
        json.dump(D,f)
        f.close()
        self.logText('\nParameters saved\n')
        
        
    def load_parameters_from_file(self):
        try:
            f=open(self.ParametersFileName)
            Dicts=json.load(f)
            f.close()
            
               
            if Dicts is not None:
                try:
                    set_parameters(self.params.it,Dicts['it'])
                    set_parameters(self.params.record,Dicts['recording'])
                    set_widget_values(self, Dicts['main_window'])
                    
                except KeyError as e:
                    self.logWarningText(str(e))
                    pass
                self.logText('\nParameters loaded\n')
    
        except FileNotFoundError:
            self.logWarningText('Error while load parameters: Parameters file not found')
    
        except json.JSONDecodeError:
            self.logWarningText('Errpr while load parameters: file has wrong format')
    
    def delete_all_figures(self):
        if plt.get_backend()!='TkAgg':  
            for i in plt.get_fignums():
                plt.close(i)
        else:
            self.logWarningText('Deleting figures does not work with TKinter backend')
        # if plt.get_backend()!='TkAgg':
        #     plt.close(plt.close('all'))
        # else:
        #     matplotlib.use("Agg")
        #     plt.close(plt.close('all'))
        #     time.sleep(0.5)
         #     matplotlib.use("TkAgg")
         
        
    def __del__(self):
        if self.it!=None:
            self.it._sock.shutdown(2)
            self.it._sock.close()
            del self.it
           
       
    
def get_widget_values(window)->dict:
    '''
    collect all data from all widgets in a window
    '''
    D={}
    for w in window.findChildren(QLineEdit):
        s=w.text()
        key=w.objectName().split('lineEdit_')[1]
        try:
            f=int(s)
            
        except ValueError:
            
            try:
                f=float(s)
                
            except ValueError:
                f=s
        D[key]=f
    for w in window.findChildren(QCheckBox):
        f=w.isChecked()
        key=w.objectName().split('checkBox_')[1]
        D[key]=f
        
    for w in window.findChildren(QComboBox):
        s=w.currentText()
        key=w.objectName().split('comboBox_')[1]
        D[key]=s
    return D

def set_widget_values(window,d:dict)->None:
     for w in window.findChildren(QLineEdit):
         key=w.objectName().split('lineEdit_')[1]
         try:
             s=d[key]
             w.setText(str(s))
         except KeyError as e:
             print('Set widget values error: '+ str(e))
             pass
     for w in window.findChildren(QCheckBox):
         key=w.objectName().split('checkBox_')[1]
         try:
             s=d[key]
             w.setChecked(s)
             w.clicked.emit(s)
         except KeyError as e:
             print('Set widget values error: '+ str(e))
     for w in window.findChildren(QComboBox):
         key=w.objectName().split('comboBox_')[1]
         try:
             s=d[key]
             w.setCurrentText(s)
         except KeyError:
             pass
    
def set_parameters(obj, d:dict):
    for key in d:
        try:
            
            if '[' in d[key]:
                d[key]=json.loads(d[key])
            obj.__setattr__(key, d[key])
        except TypeError:
            obj.__setattr__(key, d[key])
            pass
   
def get_parameters(obj) -> dict:
    '''
    Returns
    -------
    Seriazible attributes of the  object
    '''
    d = dict(vars(obj)).copy()  # make a copy of the vars dictionary
    return d


