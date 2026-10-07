"""Floating, non-modal CSV Importer window."""
import os
import shutil
import tempfile
from urllib.parse import quote

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import QDir, Qt, QUrl
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import csv_utils as cu

COL_FILE, COL_OUT, COL_X, COL_Y, COL_STATUS = range(5)
FORMATS = (
    ("GeoPackage (.gpkg)", "GPKG", (".gpkg",)),
    ("Shapefile (.shp)", "ESRI Shapefile", (".shp", ".shx", ".dbf", ".prj", ".cpg")),
)
CHOOSE = "— choose —"
DEFAULT_GPKG_NAME = "csv_import"


def _writer_no_error():
    try:
        return QgsVectorFileWriter.WriterError.NoError
    except AttributeError:  # older binding style
        return QgsVectorFileWriter.NoError


def _action_overwrite_layer():
    try:
        return QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    except AttributeError:
        return QgsVectorFileWriter.CreateOrOverwriteLayer


class OutputCell(QWidget):
    """Editable output path + browse button, shown inside the table."""

    def __init__(self, stem, parent=None):
        super().__init__(parent)
        self.stem = stem
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.setSpacing(3)
        self.edit = QLineEdit()
        self.edit.setClearButtonEnabled(False)
        self.button = QToolButton()
        self.button.setText("…")
        self.button.setToolTip("Choose output folder and file name")
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.button)
        self.edit.textChanged.connect(lambda t: self.edit.setToolTip(t))

    def set_path(self, path):
        self.edit.setText(path)
        self.edit.setCursorPosition(len(path))


class CsvImporterDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._crs_touched = False
        self._programmatic = False

        self.setWindowTitle("CSV Importer")
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setModal(False)
        self.setAcceptDrops(True)
        self.resize(1000, 620)

        layout = QVBoxLayout(self)

        self.drop_label = QLabel("Drag & drop CSV files here")
        self.drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_label.setMinimumHeight(48)
        self.drop_label.setStyleSheet(
            "QLabel { border: 2px dashed palette(mid); border-radius: 6px; color: palette(mid); }"
        )
        layout.addWidget(self.drop_label)

        buttons = QHBoxLayout()
        self.add_btn = QPushButton("Add files…")
        self.remove_btn = QPushButton("Remove selected")
        self.clear_btn = QPushButton("Clear")
        for b in (self.add_btn, self.remove_btn, self.clear_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["CSV file", "Output", "X / Longitude / Easting", "Y / Latitude / Northing", "Status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setAcceptDrops(False)  # let the dialog handle drops
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_OUT, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_X, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_Y, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        self.table.setColumnWidth(COL_FILE, 150)
        self.table.setColumnWidth(COL_X, 160)
        self.table.setColumnWidth(COL_Y, 160)
        self.table.setColumnWidth(COL_STATUS, 240)
        layout.addWidget(self.table, 1)

        form = QFormLayout()
        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        form.addRow("Coordinate system", self.crs_widget)
        self.hint = QLabel("")
        self.hint.setStyleSheet("color: #b36b00;")
        self.hint.setWordWrap(True)
        self.hint.hide()
        form.addRow("", self.hint)

        self.format_combo = QComboBox()
        for label, driver, _exts in FORMATS:
            self.format_combo.addItem(label, driver)
        form.addRow("Save as", self.format_combo)

        self.single_check = QCheckBox("Export all CSVs into a single GeoPackage")
        form.addRow("", self.single_check)

        self.single_widget = QgsFileWidget()
        self.single_widget.setStorageMode(QgsFileWidget.StorageMode.SaveFile)
        self.single_widget.setFilter("GeoPackage (*.gpkg)")
        self.single_widget.setDialogTitle("Output GeoPackage")
        if hasattr(self.single_widget, "setConfirmOverwrite"):
            self.single_widget.setConfirmOverwrite(False)  # existing file gets new layers added
        self.single_label = QLabel("GeoPackage file")
        form.addRow(self.single_label, self.single_widget)
        self.single_note = QLabel("Each CSV becomes its own layer. If the file already exists, the new "
                                  "layers are added to it; existing layers are never overwritten.")
        self.single_note.setStyleSheet("color: palette(mid);")
        self.single_note.setWordWrap(True)
        form.addRow("", self.single_note)
        for w in (self.single_widget, self.single_label, self.single_note):
            w.hide()

        self.add_to_map = QCheckBox("Add results to the map")
        self.add_to_map.setChecked(True)
        form.addRow("", self.add_to_map)
        layout.addLayout(form)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.import_btn = QPushButton("Import")
        self.import_btn.setDefault(True)
        close_btn = QPushButton("Close")
        bottom.addWidget(self.import_btn)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        self.add_btn.clicked.connect(self._browse)
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn.clicked.connect(self._clear)
        self.import_btn.clicked.connect(self._import_all)
        close_btn.clicked.connect(self.close)
        self.crs_widget.crsChanged.connect(self._on_crs_changed)
        self.format_combo.currentIndexChanged.connect(self._refresh_paths)
        self.single_check.toggled.connect(self._on_single_toggled)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _project_folder():
        return QgsProject.instance().homePath() or QDir.homePath()

    def _suggest_folder(self):
        """Folder of the first CSV in the list, else the project/home folder."""
        if self.table.rowCount():
            return self._entry(0)["folder"]
        return self._project_folder()

    def _ext(self):
        return FORMATS[self.format_combo.currentIndex()][2][0]

    def _is_single(self):
        return self.single_check.isChecked()

    def _clear(self):
        self.table.setRowCount(0)
        self.hint.hide()

    # ------------------------------------------------------------ output paths
    def _normalise_output(self, text, default_stem, base_folder):
        """Turn whatever was typed into <folder>/<safe name><.gpkg|.shp>."""
        text = os.path.expanduser((text or "").strip())
        folder, base = os.path.split(text)
        if os.path.splitext(base)[1].lower() in (".gpkg", ".shp", ".csv"):
            base = os.path.splitext(base)[0]
        stem = cu.sanitize_filename(base, default=default_stem)
        if not folder:
            folder = base_folder
        elif not os.path.isabs(folder):
            folder = os.path.join(base_folder, folder)
        return os.path.join(os.path.normpath(folder), stem + self._ext())

    def _cell(self, row):
        return self.table.cellWidget(row, COL_OUT)

    def _refresh_paths(self, *_args):
        """Re-apply the file extension after the format changed."""
        for r in range(self.table.rowCount()):
            cell = self._cell(r)
            cell.set_path(self._normalise_output(cell.edit.text(), cell.stem, self._entry(r)["folder"]))

    def _output_edited(self, row_cell):
        row = self._row_of(row_cell)
        if row < 0:
            return
        row_cell.set_path(self._normalise_output(row_cell.edit.text(), row_cell.stem,
                                                 self._entry(row)["folder"]))

    def _row_of(self, cell):
        for r in range(self.table.rowCount()):
            if self._cell(r) is cell:
                return r
        return -1

    def _browse_output(self, cell):
        label, _driver, exts = FORMATS[self.format_combo.currentIndex()]
        path, _ = QFileDialog.getSaveFileName(
            self, "Output file", cell.edit.text(), f"{label} (*{exts[0]})",
            options=QFileDialog.Option.DontConfirmOverwrite)  # existing files are never overwritten
        if path:
            cell.set_path(path)
            self._output_edited(cell)

    def _on_single_toggled(self, checked):
        self.table.setColumnHidden(COL_OUT, checked)
        for w in (self.single_widget, self.single_label, self.single_note):
            w.setVisible(checked)
        if checked:
            self.format_combo.setCurrentIndex(0)  # GeoPackage
        self.format_combo.setEnabled(not checked)
        if checked and not self.single_widget.filePath().strip():
            self.single_widget.setFilePath(os.path.join(self._suggest_folder(), DEFAULT_GPKG_NAME + ".gpkg"))

    def _on_crs_changed(self, *_args):
        if not self._programmatic:
            self._crs_touched = True
            self.hint.hide()

    # ------------------------------------------------------------ drag & drop
    @staticmethod
    def _local_csvs(mime):
        paths = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    p = url.toLocalFile()
                    if os.path.isfile(p) and p.lower().endswith(cu.CSV_EXTENSIONS):
                        paths.append(p)
        return paths

    def dragEnterEvent(self, event):
        if self._local_csvs(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = self._local_csvs(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._add_files(paths)
        else:
            event.ignore()

    # ------------------------------------------------------------ file list
    def _browse(self):
        start = self._suggest_folder()
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select CSV files", start, "CSV / text files (*.csv *.txt *.tsv *.tab)")
        if paths:
            self._add_files(paths)

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        if not self.table.rowCount():
            self.hint.hide()

    def _entry(self, row):
        return self.table.item(row, COL_FILE).data(Qt.ItemDataRole.UserRole)

    def _add_files(self, paths):
        existing = {os.path.normcase(os.path.abspath(self._entry(r)["path"]))
                    for r in range(self.table.rowCount())}
        problems = []
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if key in existing:
                continue
            try:
                info = cu.read_sample(path)
            except ValueError as exc:
                problems.append(f"{os.path.basename(path)}: {exc}")
                continue
            existing.add(key)
            self._append_row(path, info)
        if problems:
            self.iface.messageBar().pushWarning("CSV Importer", "Skipped — " + "; ".join(problems))

    def _combo(self, info, preselect):
        combo = QComboBox()
        combo.setMinimumContentsLength(8)
        combo.addItem(CHOOSE, -1)
        for idx, name in enumerate(info.header):
            if name:
                combo.addItem(name, idx)
        if preselect >= 0:
            combo.setCurrentIndex(combo.findData(preselect))
        return combo

    def _append_row(self, path, info):
        xi, yi, kind = cu.detect_xy(info.header)
        row = self.table.rowCount()
        self.table.insertRow(row)

        stem = cu.sanitize_filename(os.path.splitext(os.path.basename(path))[0])
        folder = os.path.dirname(os.path.abspath(path))

        file_item = QTableWidgetItem(os.path.basename(path))
        file_item.setToolTip(path)
        file_item.setData(Qt.ItemDataRole.UserRole, {
            "path": path, "folder": folder, "stem": stem, "encoding": info.encoding,
            "bom": info.bom, "delimiter": info.delimiter, "rows": info.rows})
        self.table.setItem(row, COL_FILE, file_item)

        cell = OutputCell(stem)
        cell.set_path(os.path.join(folder, stem + self._ext()))
        cell.edit.editingFinished.connect(lambda c=cell: self._output_edited(c))
        cell.button.clicked.connect(lambda _checked=False, c=cell: self._browse_output(c))
        self.table.setCellWidget(row, COL_OUT, cell)
        self.table.setCellWidget(row, COL_X, self._combo(info, xi))
        self.table.setCellWidget(row, COL_Y, self._combo(info, yi))

        status = QTableWidgetItem("Ready" if xi >= 0 else "Choose the X and Y fields")
        status.setToolTip(status.text())
        self.table.setItem(row, COL_STATUS, status)

        if not self._crs_touched:
            if kind == "latlon":
                self._programmatic = True
                self.crs_widget.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
                self._programmatic = False
                self.hint.hide()
            elif kind in ("en", "xy"):
                self.hint.setText("Easting/Northing (or X/Y) columns found — please choose the "
                                  "coordinate system of your data.")
                self.hint.show()

    def _set_status(self, row, text, tooltip=""):
        item = self.table.item(row, COL_STATUS)
        item.setText(text)
        item.setToolTip(tooltip or text)

    # ------------------------------------------------------------ import
    def _prepare_single(self):
        """Validate the single GeoPackage target. Returns (context, error)."""
        text = self.single_widget.filePath().strip()
        if not text:
            return None, "Choose a location and file name for the GeoPackage."
        text = os.path.expanduser(text)
        folder, base = os.path.split(text)
        stem = cu.sanitize_filename(os.path.splitext(base)[0] if base.lower().endswith(".gpkg") else base,
                                    default=DEFAULT_GPKG_NAME)
        if not folder:
            folder = self._project_folder()
        elif not os.path.isabs(folder):
            folder = os.path.join(self._project_folder(), folder)
        folder = os.path.normpath(folder)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            return None, f"Cannot create folder: {exc.strerror or exc}"
        if not os.access(folder, os.W_OK):
            return None, "The GeoPackage folder is not writable."
        path = os.path.join(folder, stem + ".gpkg")
        if not cu.is_inside(folder, path):
            return None, "Invalid GeoPackage name."
        return {"path": path, "taken": cu.gpkg_layer_names(path)}, ""

    def _import_all(self):
        if self.table.rowCount() == 0:
            self.iface.messageBar().pushInfo("CSV Importer", "Add one or more CSV files first.")
            return
        crs = self.crs_widget.crs()
        if not crs.isValid():
            self.iface.messageBar().pushWarning("CSV Importer", "Please choose a valid coordinate system.")
            return
        single = None
        if self._is_single():
            single, error = self._prepare_single()
            if single is None:
                self.iface.messageBar().pushWarning("CSV Importer", error)
                return
            driver, exts = "GPKG", (".gpkg",)
        else:
            _label, driver, exts = FORMATS[self.format_combo.currentIndex()]
        ok_count = 0
        self.setEnabled(False)  # blocks edits/drops while the batch runs
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for row in range(self.table.rowCount()):
                self._set_status(row, "Working…")
                QApplication.processEvents()
                try:
                    ok, msg = self._import_row(row, driver, exts, crs, single)
                except Exception as exc:  # never let one bad file stop the batch
                    ok, msg = False, f"✗ Unexpected error: {exc}"
                ok_count += bool(ok)
                self._set_status(row, msg)
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

        total = self.table.rowCount()
        text = f"Imported {ok_count} of {total} file(s)"
        if single and ok_count:
            text += f" into {single['path']}"
        if ok_count == total:
            self.iface.messageBar().pushSuccess("CSV Importer", text)
        else:
            self.iface.messageBar().pushWarning("CSV Importer", text + " — see the Status column.")

    def _build_uri(self, entry, x_name, y_name, decimal_comma, crs):
        crs_def = crs.authid() or ("WKT:" + crs.toWkt())
        # The provider only decodes unreserved characters, so keep harmless reserved ones
        # (brackets, colon, comma...) literal; encode only what could break the query string.
        q = lambda s: quote(s, safe="()!*',;:@$/[]\"")  # noqa: E731
        parts = [
            "type=csv", f"delimiter={q(entry['delimiter'])}", "encoding=UTF-8",
            "detectTypes=yes", "useHeader=yes", "geomType=point",
            f"xField={q(x_name)}", f"yField={q(y_name)}", f"crs={q(crs_def)}",
            "spatialIndex=no", "subsetIndex=no", "watchFile=no",
        ]
        if decimal_comma:
            parts.append("decimalPoint=%2C")
        base = bytes(QUrl.fromLocalFile(entry["path"]).toEncoded()).decode("ascii")
        return base + "?" + "&".join(parts)

    def _import_row(self, row, driver, exts, crs, single):
        entry = dict(self._entry(row))
        tmpdir = None
        try:
            # Non-UTF-8 files (and files with a BOM) are first copied as clean UTF-8, because
            # QGIS cannot always find Windows codecs by name.
            if os.path.isfile(entry["path"]) and (entry["encoding"] != "UTF-8" or entry.get("bom")):
                tmpdir = tempfile.mkdtemp(prefix="csvimporter_")
                clean = os.path.join(tmpdir, "input.csv")
                cu.write_clean_utf8(entry["path"], clean, entry["encoding"], entry.get("bom", False))
                entry["path"], entry["encoding"], entry["bom"] = clean, "UTF-8", False
            return self._import_row_inner(row, driver, exts, crs, entry, single)
        except OSError as exc:
            return False, f"✗ File error: {exc.strerror or exc}"
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    def _resolve_target(self, row, entry, exts, single):
        """Return (out_path, layer_stem, error)."""
        if single:
            stem = cu.unique_name(entry["stem"], single["taken"])  # layers never collide
            return single["path"], stem, ""
        cell = self._cell(row)
        target = self._normalise_output(cell.edit.text(), cell.stem, entry["folder"])
        folder = os.path.dirname(target)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            return "", "", f"✗ Cannot create folder: {exc.strerror or exc}"
        if not os.access(folder, os.W_OK):
            return "", "", "✗ Output folder is not writable"
        stem = os.path.splitext(os.path.basename(target))[0]
        stem = cu.unique_stem(folder, stem, exts)   # never overwrite existing data
        out_path = os.path.join(folder, stem + exts[0])
        if not cu.is_inside(folder, out_path):
            return "", "", "✗ Invalid output name"
        return out_path, stem, ""

    def _import_row_inner(self, row, driver, exts, crs, entry, single):
        x_combo, y_combo = self.table.cellWidget(row, COL_X), self.table.cellWidget(row, COL_Y)
        xi, yi = x_combo.currentData(), y_combo.currentData()
        if xi is None or yi is None or xi < 0 or yi < 0:
            return False, "✗ Choose the X and Y fields"
        if xi == yi:
            return False, "✗ X and Y must be different fields"
        if not os.path.isfile(entry["path"]):
            return False, "✗ CSV file no longer exists"

        out_path, stem, error = self._resolve_target(row, entry, exts, single)
        if error:
            return False, error

        decimal_comma = cu.uses_decimal_comma(entry["rows"], (xi, yi), entry["delimiter"])
        uri = self._build_uri(entry, x_combo.currentText(), y_combo.currentText(), decimal_comma, crs)
        layer = QgsVectorLayer(uri, stem, "delimitedtext")
        if not layer.isValid():
            return False, "✗ Could not read the CSV with these settings"
        imported = layer.featureCount()
        if imported == 0:
            return False, "✗ No valid points (check the X/Y fields, delimiter and coordinate system)"

        warning = ""
        if crs.isGeographic():
            e = layer.extent()
            if e.xMinimum() < -180.001 or e.xMaximum() > 180.001 or e.yMinimum() < -90.001 or e.yMaximum() > 90.001:
                warning = " ⚠ values are outside lon/lat range — check the coordinate system"

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver
        options.fileEncoding = "UTF-8"
        if driver == "GPKG":
            options.layerName = stem
            if single and os.path.exists(out_path):
                options.actionOnExistingFile = _action_overwrite_layer()  # add layer, keep the others
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, out_path, QgsProject.instance().transformContext(), options)
        err, err_msg = result[0], result[1]
        if err != _writer_no_error():
            return False, f"✗ Save failed: {err_msg}"
        if single:
            single["taken"].add(stem.lower())

        try:
            total = cu.count_rows(entry["path"], entry["encoding"], entry["delimiter"])
        except Exception:
            total = imported
        del layer  # release the file handle

        if self.add_to_map.isChecked():
            src = f"{out_path}|layername={stem}" if driver == "GPKG" else out_path
            result_layer = QgsVectorLayer(src, stem, "ogr")
            if result_layer.isValid():
                QgsProject.instance().addMapLayer(result_layer)

        name = f"layer “{stem}”" if single else os.path.basename(out_path)
        msg = f"✓ {name} — {imported} of {total} rows"
        if total > imported:
            msg += f" ({total - imported} skipped: missing/invalid coordinates)"
        return True, msg + warning
