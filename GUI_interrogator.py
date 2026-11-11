
from MainWindow import MainWindow
from PyQt5 import QtWidgets, QtGui
from packaging import version
import os
import sys
__version__ = '2.1.0'
__date__ = '2026.11.11'

def resource_path(name):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# from conda import __version__ as condaVersion



def main():
    if not QtWidgets.QApplication.instance():
        app = QtWidgets.QApplication(sys.argv) 
    else:
        app = QtWidgets.QApplication.instance()
    app.setWindowIcon(QtGui.QIcon(resource_path('app_icon.ico')))
    main_app = MainWindow(version=__version__, date=__date__)
    main_app.show()
    # Uncomment only if application doesn't finish properly
    sys.exit(app.exec())
    return main_app
 

if __name__ == '__main__':
    m = main()


