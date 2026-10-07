"""Plugin entry point: menu/toolbar action that opens the floating window."""
import os

from qgis.PyQt.QtGui import QIcon

try:  # Qt6 (QGIS 4) moved QAction to QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from .dialog import CsvImporterDialog

MENU = "&CSV Importer"


class CsvImporterPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "CSV Importer", self.iface.mainWindow())
        self.action.setToolTip("Import CSV files as point layers")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(MENU, self.action)
        self.iface.addToolBarIcon(self.action)

    def run(self, *_args):
        if self.dialog is None:
            self.dialog = CsvImporterDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
