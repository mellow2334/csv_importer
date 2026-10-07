def classFactory(iface):
    from .plugin import CsvImporterPlugin
    return CsvImporterPlugin(iface)
