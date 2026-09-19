from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from davincibot.models import TemplateManifest, WorkflowKind, WorkspaceMode, WorkspaceProfile
from davincibot.templates import slots_from_markers


class FolderDialog(QDialog):
    def __init__(self, profile: WorkspaceProfile, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Folders — {profile.name}")
        self.profile = profile.model_copy(deep=True)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(len(profile.folders), 3)
        self.table.setHorizontalHeaderLabels(["Role", "Folder", "Choose"])
        for row, folder in enumerate(profile.folders):
            role = QTableWidgetItem(folder.role.value)
            role.setFlags(role.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, role)
            self.table.setItem(row, 1, QTableWidgetItem(str(folder.path)))
            button = QPushButton("Browse…")
            button.clicked.connect(lambda _checked=False, r=row: self._browse(r))
            self.table.setCellWidget(row, 2, button)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(1, 520)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self, row: int) -> None:
        start = self.table.item(row, 1).text()
        selected = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if selected:
            self.table.item(row, 1).setText(selected)

    def result_profile(self) -> WorkspaceProfile:
        for row, folder in enumerate(self.profile.folders):
            folder.path = Path(self.table.item(row, 1).text()).resolve()
        return self.profile


class TemplateDialog(QDialog):
    def __init__(
        self,
        timeline_data: dict | None,
        mode: WorkspaceMode,
        parent=None,
        previous: TemplateManifest | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Register Resolve template")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.timeline = QLineEdit(timeline_data.get("name", "") if timeline_data else "")
        self.workflow = QComboBox()
        for item in WorkflowKind:
            self.workflow.addItem(item.value.replace("_", " ").title(), item)
        form.addRow("Template name", self.name)
        form.addRow("Resolve timeline", self.timeline)
        form.addRow("Workflow", self.workflow)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.timeline_data = timeline_data or {"markers": {}}
        self.mode = mode
        self.previous = previous
        self.resize(750, 700)
        base = previous or TemplateManifest(
            name="New template",
            timeline_name=self.timeline.text(),
            workflows={self.workflow.currentData()},
            modes={mode},
            aspect_ratios={"9:16", "16:9"},
            slots=slots_from_markers(self.timeline_data.get("markers", {})),
            track_contract=self.timeline_data.get("tracks", {}),
            frame_rate=float(timeline_data["frame_rate"])
            if timeline_data and timeline_data.get("frame_rate")
            else None,
            parameters={
                "font": "Arial",
                "font_size": 0.045,
                "punch_in_zoom": 1.15,
                "transition": "cut",
            },
        )
        self.base = base
        self.name.setText(base.name)
        self.timeline.setText(base.timeline_name)
        self.workflow.setCurrentIndex(self.workflow.findData(sorted(base.workflows)[0]))
        self.advanced = QPlainTextEdit(
            json.dumps(
                base.model_dump(
                    mode="json", exclude={"id", "version", "name", "timeline_name", "workflows"}
                ),
                indent=2,
            )
        )
        form.insertRow(3, "Settings / slots / fonts / snapshot (JSON)", self.advanced)
        browse = QPushButton("Choose exported Resolve .drt snapshot")
        browse.clicked.connect(self._choose_snapshot)
        form.insertRow(4, browse)

    def _choose_snapshot(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Template snapshot", "", "Resolve timeline (*.drt)"
        )
        if filename:
            try:
                data = json.loads(self.advanced.toPlainText())
                data["source_snapshot"] = filename
                self.advanced.setPlainText(json.dumps(data, indent=2))
            except ValueError:
                QMessageBox.warning(self, "Invalid JSON", "Correct the settings JSON first.")

    def accept(self):
        from PySide6.QtGui import QFontDatabase

        from davincibot.templates import validate_template

        try:
            template = self.manifest()
            result = validate_template(template)
            fonts = set(template.fonts) | {template.parameters.get("font", "Arial")}
            installed = {name.casefold() for name in QFontDatabase.families()}
            result.errors.extend(
                f"Font not installed: {font}" for font in fonts if font.casefold() not in installed
            )
            if result.errors:
                raise ValueError("\n".join(result.errors))
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "Template validation", str(error))
            return
        super().accept()

    def manifest(self) -> TemplateManifest:
        data = json.loads(self.advanced.toPlainText())
        data.update(
            id=self.base.id,
            version=self.base.version + 1 if self.previous else 1,
            name=self.name.text().strip(),
            timeline_name=self.timeline.text().strip(),
            workflows=[self.workflow.currentData()],
        )
        return TemplateManifest.model_validate(data)


class JsonDialog(QDialog):
    def __init__(self, title, value, validator, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(800, 650)
        self.validator = validator
        self.value = None
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(json.dumps(value, indent=2, ensure_ascii=False))
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        try:
            self.value = self.validator(json.loads(self.editor.toPlainText()))
        except (ValueError, TypeError, KeyError) as error:
            QMessageBox.warning(self, "Validation", str(error))
            return
        super().accept()
