# -*- coding: utf-8 -*-
"""
Created on Wed Jan 21 11:32:18 2026

@author: Илья
"""

__version__='1.0'
__date__='2026.21.01'

import os
    
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
import sys
import json
import time 
import pickle

from PyQt5.QtCore import pyqtSignal, QThread
from PyQt5.QtWidgets import QMainWindow, QFileDialog, QDialog,QLineEdit,QComboBox,QCheckBox,QMessageBox

import importlib


from FBGRecorder import record_and_plot,record_to_file,read_fbg_stream_raw_lp
from interrogator import Interrogator



from UIs.MainWindowUI import Ui_MainWindow


class Parameters():
    def __init__(self):
        self.it_IP=None
        self.PC_IP=None
        self.FBGs=None
        self.channels=None
        self.gains_auto=None
        self.gains_manual=None
        self.thresholds=None
        self.rep_rate=None
        self.recording_duration=None
        self.write_every_nth=None
        
    def set_parameters(self, d:dict):
        for key in d:
            try:
                
                if '[' in d[key]:
                    d[key]=json.loads(d[key])
                self.__setattr__(key, d[key])
            except TypeError:
                self.__setattr__(key, d[key])
                pass
   
    def get_parameters(self) -> dict:
        '''
        Returns
        -------
        Seriazible attributes of the  object
        '''
        d = dict(vars(self)).copy()  # make a copy of the vars dictionary
        return d

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
        
        self.parameters=Parameters()
        self.load_parameters_from_file()
        
        self.it=None
        
  
        
    def logText(self, text):
        self.ui.LogField.append(">" + text)
        
    def logWarningText(self, text):
        self.ui.LogField.append("<span style=\" font-size:8pt; font-weight:600; color:#ff0000;\" >"
                             + ">" + text + "</span>")
        
    def init_interface(self):
        self.ui.pushButton_start_recording.pressed.connect(self.recording)
        self.ui.pushButton_set_parameters.pressed.connect(self.on_pushButton_set_parameters)
        self.ui.pushButton_connect.pressed.connect(self.connect_interrogator)
        self.ui.pushButton_choose_file_to_load.clicked.connect(self.choose_file_to_load)
        self.ui.pushButton_choose_folder_to_save.clicked.connect(self.choose_folder_to_save)
        self.ui.pushButton_plot_from_file.clicked.connect(self.plot_from_file)
        self.ui.pushButton_plot_single_spectrum.clicked.connect(self.plot_single_spectrum)
        
        
    
        
    def connect_interrogator(self):
        try:
            self.it = Interrogator(self.parameters.it_IP,self.parameters.PC_IP)
            self.set_gains()
            # self.add_thread(self.it)
            self.logText('Connected to interrogator')
        except Exception as e:
            self.logWarningText(str(e))
            
    def set_gains(self):
        for ch in range(4):
            self.it.set_gain(ch+1, auto=self.parameters.gains_auto[ch], manual_level=self.parameters.gains_manual[ch])
            self.it.set_threshold(ch+1, self.parameters.thresholds[ch])

    def plot_single_spectrum(self):
        waves=self.it.get_waves()
        for ch in self.parameters.channels:
            spectrum=self.it.get_single_spectrum(ch)
            plt.figure()
            plt.plot(waves,spectrum)
            plt.xlabel('Wavelength, nm')
            plt.ylabel('Spectral power, dBm')
            plt.title('Channel {}'.format(ch))


    def recording(self):
        FilePrefix=self.ui.lineEdit_file_name.text()
        self.logText('start recording')
        
        try:
            self.it.start_freq_stream(self.parameters.rep_rate)
            stats = record_to_file(self.it, self.saving_dir_path+FilePrefix+".pkl", duration_sec=self.parameters.recording_duration,
                                   channels=self.parameters.channels,FBGs=self.parameters.FBGs,write_every_n=self.parameters.write_every_nth)
            self.logText("Rcording finished: {}".format(stats))
            self.it.stop_freq_stream()
        except Exception as e:
            self.logWarningText(str(e))
            
        self.it.stop_freq_stream()
        
        
    def plot_from_file(self):
        
        times, channels, channel_list, FBGs_list = read_fbg_stream_raw_lp(self.file_to_load_path)
        self.logText('In this file there are channels {} and FBGs {} in these channels'.format(channel_list,FBGs_list))
        for ch in self.parameters.channels:
            for FBGs in self.parameters.FBGs:
                for FBG in FBGs:
                    plt.figure()
                    plt.plot(times - times[0], channels[ch][FBG])
                    plt.xlabel('Time, s')
                    plt.ylabel('FBG wavelength, nm')
                    plt.title('ch {} FBG {}'.format(ch,FBG))
        
        

        
        
    def on_pushButton_set_parameters(self):
        '''
        open dialog with analyzer parameters
        '''
        d = self.parameters.get_parameters()
        from UIs.parameters_dialogUI import Ui_Dialog
        parameters_dialog = QDialog()
        ui = Ui_Dialog()
        ui.setupUi(parameters_dialog)
        set_widget_values(parameters_dialog,d)
        if parameters_dialog.exec_() == QDialog.Accepted:
            params=get_widget_values(parameters_dialog)
            self.parameters.set_parameters(params)
            if self.it!=None:
                self.set_gains()
            
        

            
    
    def init_menu_bar(self):
        self.ui.action_save_parameters.triggered.connect(self.save_parameters_to_file)
        self.ui.action_load_parameters.triggered.connect(self.load_parameters_from_file)
        
        
    def choose_file_to_load(self):
        DataFilePath= str(QFileDialog.getOpenFileName(self, "Select Data File",'','*.pkl ' )).split("\',")[0].split("('")[1]
        if DataFilePath=='':
            self.logWarningText('file is not chosen or previous choice is preserved')
        self.file_to_load_path=DataFilePath
        self.ui.label_file_to_load.setText(DataFilePath)
        
        
    def choose_folder_to_save(self):
        self.saving_dir_path = str(
            QFileDialog.getExistingDirectory(self, "Select Directory"))+'\\'
        self.ui.label_folder_to_save.setText(self.spectral_processor.source_dir_path+'\\')
        
        
    def save_parameters_to_file(self):
        '''
        save all parameters and values except paths to file

        Returns
        -------
        None.

        '''
        D={}
        D['parameters']=self.parameters.get_parameters()

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
    
        except FileNotFoundError:
            self.logWarningText('Error while load parameters: Parameters file not found')
    
        except json.JSONDecodeError:
            self.logWarningText('Errpr while load parameters: file has wrong format')
    
        
        if Dicts is not None:
            try:
                self.parameters.set_parameters(Dicts['parameters'])
                
            except KeyError:
                pass
            self.logText('\nParameters loaded\n')
            
        
    
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
    