from __future__ import annotations

import json
from uuid import uuid4

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from davincibot.assets import AssetScanner, ScanResult, partition_batches
from davincibot.cache import ManagedCache
from davincibot.database import Database
from davincibot.jobs import JobService
from davincibot.models import (
    AssetRole,
    EditPlan,
    JobRecord,
    JobSpec,
    JobState,
    ProviderKind,
    ResourceMode,
    TemplateManifest,
    WorkspaceProfile,
)
from davincibot.paths import AppPaths
from davincibot.planner.providers import make_provider
from davincibot.planner.service import PlanningService
from davincibot.preflight import PreflightReport, run_preflight
from davincibot.resolve.bridge import DirectResolveBridge, FileResolveBridge, ResolveBuildError
from davincibot.resolve.install import install_launcher
from davincibot.secrets import SecretStore
from davincibot.ui.dialogs import FolderDialog, JsonDialog, TemplateDialog
from davincibot.ui.workers import Worker

DEFAULT_MODELS = {
    ProviderKind.OFFLINE: "offline-rules",
    ProviderKind.OPENAI: "gpt-5-mini",
    ProviderKind.ANTHROPIC: "claude-sonnet-4-5",
    ProviderKind.GEMINI: "gemini-2.5-flash",
    ProviderKind.OPENAI_COMPATIBLE: "model-name",
}


class MainWindow(QMainWindow):
    def __init__(self, database: Database, paths: AppPaths):
        super().__init__()
        self.database = database
        self.paths = paths
        self.scanner = AssetScanner()
        self.secrets = SecretStore()
        self.planner = PlanningService(self.secrets.get_api_key)
        self.jobs = JobService(database, paths)
        self.direct_bridge = DirectResolveBridge()
        self.file_bridge = FileResolveBridge(paths.bridge)
        self.thread_pool = QThreadPool.globalInstance()
        self.scan_result: ScanResult | None = None
        self.current_job: JobRecord | None = None
        self.current_template: TemplateManifest | None = None
        self.current_preflight: PreflightReport | None = None
        self.batch_builds: list[tuple[JobRecord, TemplateManifest, PreflightReport]] = []
        self._working = False

        self.setWindowTitle("DaVinciBot")
        self.resize(1180, 800)
        self.setCentralWidget(self._build_ui())
        self._load_profiles()
        self.refresh_jobs()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(2000)

    def _build_ui(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        edit_folders = QPushButton("Edit folders")
        edit_folders.clicked.connect(self.edit_folders)
        register_template = QPushButton("Register template")
        register_template.clicked.connect(self.register_template)
        install = QPushButton("Install Resolve Free launcher")
        install.clicked.connect(self.install_resolve_launcher)
        header.addWidget(QLabel("Workspace"))
        header.addWidget(self.profile_combo, 1)
        header.addWidget(edit_folders)
        header.addWidget(register_template)
        header.addWidget(install)
        self.header_controls = [edit_folders, register_template, install]
        settings = QPushButton("Workspace settings")
        settings.clicked.connect(self.edit_settings)
        header.addWidget(settings)
        self.header_controls.append(settings)
        cache = QPushButton("Cache usage / cleanup")
        cache.clicked.connect(self.manage_cache)
        header.addWidget(cache)
        self.header_controls.append(cache)
        layout.addLayout(header)

        form = QFormLayout()
        self.project_edit = QLineEdit()
        self.project_edit.setPlaceholderText("Exact Resolve project name")
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Describe the desired video, pacing, layout, graphics, and template style…"
        )
        self.prompt_edit.setMaximumHeight(110)
        provider_row = QHBoxLayout()
        self.provider_combo = QComboBox()
        for provider in ProviderKind:
            self.provider_combo.addItem(provider.value.replace("_", " ").title(), provider)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.model_edit = QLineEdit(DEFAULT_MODELS[ProviderKind.OFFLINE])
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("Optional compatible API base URL")
        key_button = QPushButton("Set API key")
        key_button.clicked.connect(self.set_api_key)
        provider_row.addWidget(self.provider_combo)
        provider_row.addWidget(self.model_edit)
        provider_row.addWidget(self.base_url_edit)
        provider_row.addWidget(key_button)
        form.addRow("Resolve project", self.project_edit)
        form.addRow("Prompt", self.prompt_edit)
        form.addRow("Planner", provider_row)
        template_row = QHBoxLayout()
        self.template_combo = QComboBox()
        self.template_combo.currentIndexChanged.connect(self._invalidate_plan)
        template_row.addWidget(self.template_combo, 1)
        self.edit_template_button = QPushButton("Edit template / new version")
        self.edit_template_button.clicked.connect(self.edit_template)
        template_row.addWidget(self.edit_template_button)
        self.resource_combo = QComboBox()
        for mode in ResourceMode:
            self.resource_combo.addItem(mode.value.title(), mode)
        self.resource_combo.currentIndexChanged.connect(self._resource_changed)
        template_row.addWidget(self.resource_combo)
        form.addRow("Template / resources", template_row)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.scan_button = QPushButton("1. Scan folders")
        self.plan_button = QPushButton("2. Create plan")
        self.build_button = QPushButton("3. Approve and build")
        self.bridge_combo = QComboBox()
        self.bridge_combo.addItem("Resolve Studio — direct", "direct")
        self.bridge_combo.addItem("Resolve Free — Workspace script", "file")
        self.batch_checkbox = QCheckBox("Batch by filename/subfolder")
        self.plan_button.setEnabled(False)
        self.build_button.setEnabled(False)
        self.scan_button.clicked.connect(self.scan)
        self.plan_button.clicked.connect(self.create_plan)
        self.build_button.clicked.connect(self.approve_build)
        actions.addWidget(self.scan_button)
        actions.addWidget(self.plan_button)
        actions.addWidget(self.build_button)
        actions.addWidget(self.batch_checkbox)
        actions.addStretch()
        actions.addWidget(QLabel("Bridge"))
        actions.addWidget(self.bridge_combo)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.asset_table = QTableWidget(0, 5)
        self.asset_table.setHorizontalHeaderLabels(["Role", "File", "Group", "Duration", "Status"])
        self.asset_table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.asset_table)
        self.plan_view = QPlainTextEdit()
        self.plan_view.setReadOnly(True)
        self.plan_view.setPlaceholderText(
            "The validated edit plan and preflight results appear here."
        )
        splitter.addWidget(self.plan_view)
        self.jobs_table = QTableWidget(0, 4)
        self.jobs_table.setHorizontalHeaderLabels(["Updated", "State", "Project", "Error"])
        self.jobs_table.horizontalHeader().setStretchLastSection(True)
        splitter.addWidget(self.jobs_table)
        layout.addWidget(splitter, 1)
        review_actions = QHBoxLayout()
        self.edit_plan_button = QPushButton("Edit plan / caption assignments")
        self.edit_plan_button.clicked.connect(self.edit_plan)
        self.retry_button = QPushButton("Retry with offline planner")
        self.retry_button.clicked.connect(self.retry_offline)
        self.regenerate_button = QPushButton("Review / regenerate selected job")
        self.regenerate_button.clicked.connect(self.review_job)
        cancel = QPushButton("Cancel selected job")
        cancel.clicked.connect(self.cancel_selected)
        for button in (self.edit_plan_button, self.retry_button, self.regenerate_button, cancel):
            review_actions.addWidget(button)
        layout.addLayout(review_actions)
        self.asset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.jobs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.status = QLabel("Ready")
        layout.addWidget(self.status)
        return root

    def _load_profiles(self) -> None:
        selected = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.database.list_profiles():
            self.profile_combo.addItem(f"{profile.name} — {profile.mode.value}", profile.id)
        index = self.profile_combo.findData(selected)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)
        self._profile_changed()

    def _profile(self) -> WorkspaceProfile:
        profile = self.database.get_profile(self.profile_combo.currentData())
        if not profile:
            raise RuntimeError("select a workspace profile")
        return profile

    def _profile_changed(self) -> None:
        self.scan_result = None
        self.current_job = None
        self.current_template = None
        self.current_preflight = None
        self.batch_builds = []
        self.asset_table.setRowCount(0)
        self.plan_view.clear()
        self.plan_button.setEnabled(False)
        self.build_button.setEnabled(False)
        self._load_templates()
        self.resource_combo.blockSignals(True)
        self.resource_combo.setCurrentIndex(
            self.resource_combo.findData(self._profile().resource_mode)
        )
        self.resource_combo.blockSignals(False)

    def _load_templates(self, selected=None):
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("Automatic template selection", None)
        profile = self._profile()
        for template in self.database.list_templates():
            if profile.mode in template.modes and (
                not profile.template_ids or template.id in profile.template_ids
            ):
                self.template_combo.addItem(f"{template.name} (v{template.version})", template.id)
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData(selected)))
        self.template_combo.blockSignals(False)

    def _invalidate_plan(self):
        self.current_preflight = None
        self.batch_builds = []
        self.build_button.setEnabled(False)

    def _resource_changed(self):
        profile = self._profile()
        profile.resource_mode = self.resource_combo.currentData()
        self.database.save_profile(profile)
        self._invalidate_plan()

    def edit_settings(self):
        profile = self._profile()
        dialog = JsonDialog(
            "Workspace settings",
            profile.model_dump(mode="json"),
            WorkspaceProfile.model_validate,
            self,
        )
        if dialog.exec():
            if dialog.value.id != profile.id or dialog.value.mode != profile.mode:
                self._error("Workspace identity and mode cannot be changed here.")
                return
            self.database.save_profile(dialog.value)
            self._load_profiles()

    def _provider_changed(self) -> None:
        provider = self.provider_combo.currentData()
        self.model_edit.setText(DEFAULT_MODELS[provider])
        self.base_url_edit.setVisible(provider is ProviderKind.OPENAI_COMPATIBLE)

    def edit_folders(self) -> None:
        profile = self._profile()
        dialog = FolderDialog(profile, self)
        if dialog.exec():
            self.database.save_profile(dialog.result_profile())
            self._profile_changed()

    def set_api_key(self) -> None:
        provider: ProviderKind = self.provider_combo.currentData()
        if provider is ProviderKind.OFFLINE:
            QMessageBox.information(
                self, "Offline planner", "The offline planner needs no API key."
            )
            return
        value, accepted = QInputDialog.getText(
            self,
            "Store API key",
            f"{provider.value} API key",
            QLineEdit.EchoMode.Password,
        )
        if accepted and value:
            self.secrets.set_api_key(provider, value)
            self.status.setText(f"Stored {provider.value} key in Windows Credential Manager")

    def scan(self) -> None:
        self._invalidate_plan()
        self.current_job = None
        self._busy(True, "Scanning folders and probing media…")
        worker = Worker(self.scanner.scan, self._profile(), True)
        worker.signals.succeeded.connect(self._scan_complete)
        worker.signals.failed.connect(self._error)
        worker.signals.finished.connect(lambda: self._busy(False))
        self.thread_pool.start(worker)

    def _scan_complete(self, result: ScanResult) -> None:
        self.scan_result = result
        self.asset_table.setRowCount(len(result.assets))
        for row, asset in enumerate(result.assets):
            role = QComboBox()
            for item in AssetRole:
                if item is not AssetRole.OUTPUT:
                    role.addItem(item.value, item)
            role.setCurrentIndex(role.findData(asset.role))
            self.asset_table.setCellWidget(row, 0, role)
            self.asset_table.setItem(row, 1, QTableWidgetItem(str(asset.path)))
            self.asset_table.setItem(row, 2, QTableWidgetItem(asset.group_key))
            duration = f"{asset.media.duration_seconds:.2f}s" if asset.media else "—"
            self.asset_table.setItem(row, 3, QTableWidgetItem(duration))
            self.asset_table.setItem(row, 4, QTableWidgetItem("Ready" if asset.media else "File"))
        self.plan_button.setEnabled(bool(result.assets))
        warning_text = "\n".join(result.warnings) or "No scan warnings"
        self.plan_view.setPlainText(warning_text)
        self.status.setText(
            f"Found {len(result.assets)} assets and {len(result.captions)} caption cues"
        )

    def _apply_role_overrides(self) -> None:
        if not self.scan_result:
            return
        for row, asset in enumerate(self.scan_result.assets):
            combo = self.asset_table.cellWidget(row, 0)
            if isinstance(combo, QComboBox):
                asset.role = combo.currentData()

    def create_plan(self) -> None:
        if not self.scan_result:
            return
        prompt = self.prompt_edit.toPlainText().strip()
        project = self.project_edit.text().strip()
        if not prompt or not project:
            QMessageBox.warning(self, "Missing fields", "Enter a prompt and Resolve project name.")
            return
        self._apply_role_overrides()
        profile = self._profile()
        selected = (
            self.database.get_template(self.template_combo.currentData())
            if self.template_combo.currentData()
            else None
        )
        self.batch_builds = []
        self.current_preflight = None
        spec = JobSpec(
            profile_id=profile.id,
            prompt=prompt,
            provider=self.provider_combo.currentData(),
            provider_model=self.model_edit.text().strip(),
            provider_base_url=self.base_url_edit.text().strip() or None,
            target_project=project,
            assets=self.scan_result.assets,
            captions=self.scan_result.captions,
            resource_mode=profile.resource_mode,
            profile_snapshot=profile.model_copy(deep=True),
            overrides={"template_id": selected.id, "template_version": selected.version}
            if selected
            else {},
        )
        templates = self.database.list_templates()
        self._busy(True, "Creating a schema-validated edit plan…")
        if self.batch_checkbox.isChecked():
            specs = self._batch_specs(spec)
            worker = Worker(self._plan_many, specs, profile, templates)
            worker.signals.succeeded.connect(self._batch_plan_complete)
        else:
            self.current_job = self.jobs.create(spec)
            worker = Worker(self.planner.plan, spec, profile, templates, make_provider)
            worker.signals.succeeded.connect(self._plan_complete)
        worker.signals.failed.connect(self._plan_failed)
        worker.signals.finished.connect(lambda: self._busy(False))
        self.thread_pool.start(worker)

    def _batch_specs(self, base: JobSpec) -> list[JobSpec]:
        groups = partition_batches(base.assets)
        primary_groups = {
            asset.group_key
            for asset in base.assets
            if asset.role in {AssetRole.A_ROLL, AssetRole.CAMERA, AssetRole.VOICE}
        }
        if not primary_groups:
            primary_groups = {a.group_key for a in base.assets if a.role is AssetRole.B_ROLL}
        shared_roles = {AssetRole.MUSIC, AssetRole.GRAPHIC, AssetRole.TEMPLATE}
        specs: list[JobSpec] = []
        for key in sorted(primary_groups):
            selected = [
                asset
                for asset in base.assets
                if asset.group_key == key or asset.role in shared_roles
            ]
            specs.append(
                base.model_copy(
                    update={
                        "id": str(uuid4()),
                        "assets": selected,
                        "captions": [
                            cue
                            for cue in base.captions
                            if cue.group_key == key
                            or (not cue.group_key and len(primary_groups or groups) == 1)
                        ],
                        "batch": True,
                        "overrides": {**base.overrides, "batch_group": key},
                    }
                )
            )
        return specs

    def _plan_many(self, specs, profile, templates):
        results = []
        for spec in specs:
            self.jobs.create(spec)
            try:
                plan = self.planner.plan(spec, profile, templates, make_provider)
                record = self.database.get_job(spec.id)
                record.spec = spec
                self.database.save_job(record)
                self.jobs.attach_plan(spec.id, plan)
                results.append((spec, plan))
            except Exception as error:
                self.jobs.transition(spec.id, JobState.NEEDS_ATTENTION, str(error))
        return results

    def _batch_plan_complete(self, results) -> None:
        profile = self._profile()
        self.batch_builds = []
        summaries = []
        for spec, plan in results:
            record = self.database.get_job(spec.id)
            template = self.database.get_template(plan.template_id, plan.template_version)
            if not template:
                continue
            report = run_preflight(spec, plan, spec.profile_snapshot or profile, template)
            if plan.confidence < profile.batch_confidence_threshold:
                report.errors.append(
                    "Confidence below batch threshold; review this job individually."
                )
            if not report.ready:
                self.jobs.transition(spec.id, JobState.NEEDS_ATTENTION, "; ".join(report.errors))
            self.batch_builds.append((record, template, report))
            summaries.append(
                {
                    "group": spec.overrides.get("batch_group"),
                    "job_id": spec.id,
                    "ready": report.ready,
                    "confidence": plan.confidence,
                    "errors": report.errors,
                    "warnings": report.warnings,
                    "template": template.name,
                }
            )
        ready = any(item[2].ready for item in self.batch_builds)
        self.plan_view.setPlainText(json.dumps({"batch": summaries}, indent=2))
        self.build_button.setEnabled(ready)
        self.status.setText(f"Prepared {len(self.batch_builds)} batch jobs for approval")
        self.refresh_jobs()

    def _plan_complete(self, plan) -> None:
        if not self.current_job:
            return
        if self.current_job.id != plan.job_id:
            self._error("Received a plan for a different job; no job was modified.")
            return
        latest = self.database.get_job(plan.job_id)
        if latest.state is JobState.CANCELLED:
            return
        latest.spec = self.current_job.spec
        self.database.save_job(latest)
        self.current_job = self.jobs.attach_plan(self.current_job.id, plan)
        profile = self.current_job.spec.profile_snapshot or self._profile()
        template = self.database.get_template(plan.template_id, plan.template_version)
        if not template:
            self._error("The selected template disappeared before preflight.")
            return
        self.current_template = template
        self.current_preflight = run_preflight(self.current_job.spec, plan, profile, template)
        report = {
            "preflight_ready": self.current_preflight.ready,
            "errors": self.current_preflight.errors,
            "warnings": self.current_preflight.warnings,
            "plan": plan.model_dump(mode="json"),
        }
        self.plan_view.setPlainText(json.dumps(report, indent=2, ensure_ascii=False))
        self.build_button.setEnabled(self.current_preflight.ready)
        self.status.setText(
            "Plan ready for approval" if self.current_preflight.ready else "Preflight failed"
        )
        self.refresh_jobs()

    def _plan_failed(self, message: str) -> None:
        if self.current_job:
            try:
                self.jobs.transition(self.current_job.id, JobState.FAILED, message)
            except Exception:
                pass
        self._error(message)

    def approve_build(self) -> None:
        if self.batch_builds:
            self._approve_batch()
            return
        if not self.current_job or not self.current_template or not self.current_preflight:
            return
        if not self.current_preflight.ready:
            QMessageBox.warning(self, "Preflight", "Resolve all preflight errors first.")
            return
        if (
            QMessageBox.question(
                self,
                "Build timeline",
                "Create a new versioned timeline in Resolve? The template and existing timelines "
                "will not be modified.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        bridge = (
            self.direct_bridge if self.bridge_combo.currentData() == "direct" else self.file_bridge
        )
        try:
            self.jobs.approve_and_build(self.current_job.id, bridge, self.current_template)
        except Exception as error:
            self._error(str(error))
            return
        self.build_button.setEnabled(False)
        self.current_preflight = None
        self.status.setText(
            "Build queued"
            if bridge is self.direct_bridge
            else "Queued; run the DaVinciBot script in Resolve"
        )

    def _approve_batch(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Build batch",
                f"Queue {len(self.batch_builds)} versioned timelines for Resolve?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        bridge = (
            self.direct_bridge if self.bridge_combo.currentData() == "direct" else self.file_bridge
        )
        for record, template, report in self.batch_builds:
            if report.ready and record.plan:
                try:
                    self.jobs.approve_and_build(record.id, bridge, template)
                except Exception as error:
                    self._error(str(error))
        self.build_button.setEnabled(False)
        self.status.setText(f"Queued {len(self.batch_builds)} batch jobs")
        self.batch_builds = []

    def register_template(self, _checked=False, previous=None) -> None:
        data = None
        try:
            data = self.direct_bridge.current_timeline_data()
        except ResolveBuildError:
            pass
        dialog = TemplateDialog(data, self._profile().mode, self, previous=previous)
        if dialog.exec():
            try:
                template = dialog.manifest()
                snapshot = (
                    self.paths.root / "templates" / template.id / f"v{template.version:03d}.drt"
                )
                if template.source_snapshot:
                    import shutil

                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    if snapshot.exists():
                        raise ValueError("Snapshot exists; register a new version.")
                    shutil.copyfile(template.source_snapshot, snapshot)
                    template.source_snapshot = snapshot
                elif data:
                    self.direct_bridge.export_snapshot(snapshot, template.timeline_name)
                    template.source_snapshot = snapshot
                else:
                    raise ValueError(
                        "Choose an exported .drt file, or open Resolve Studio for registration."
                    )
                import hashlib
                with snapshot.open("rb") as handle:
                    template.snapshot_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
                self.database.save_template(template)
                profile = self._profile()
                if template.id not in profile.template_ids:
                    profile.template_ids.append(template.id)
                    self.database.save_profile(profile)
                self._invalidate_plan()
                self._load_templates(template.id)
                self.status.setText(f"Registered template {template.name} v{template.version}")
            except Exception as error:
                self._error(str(error))

    def install_resolve_launcher(self) -> None:
        try:
            target = install_launcher()
            QMessageBox.information(
                self,
                "Launcher installed",
                f"Installed {target}. Restart Resolve to refresh its Scripts menu.",
            )
        except OSError as error:
            self._error(str(error))

    def refresh_jobs(self) -> None:
        selected = self._selected_job_id()
        records = self.database.list_jobs()
        self.jobs_table.setRowCount(len(records))
        for row, record in enumerate(records):
            self.jobs_table.setItem(row, 0, QTableWidgetItem(str(record.updated_at)[:19]))
            self.jobs_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, record.id)
            self.jobs_table.setItem(row, 1, QTableWidgetItem(record.state.value))
            self.jobs_table.setItem(row, 2, QTableWidgetItem(record.spec.target_project))
            self.jobs_table.setItem(row, 3, QTableWidgetItem(record.error or ""))
            if selected == record.id:
                self.jobs_table.selectRow(row)

    def _poll(self) -> None:
        try:
            changed = self.jobs.sync_file_bridge(self.file_bridge)
            if changed:
                self.status.setText(f"Resolve completed {len(changed)} queued job(s)")
            self.refresh_jobs()
        except Exception as error:
            self.status.setText(f"Bridge polling warning: {error}")

    def _busy(self, busy: bool, message: str | None = None) -> None:
        self._working = busy
        for control in [
            self.profile_combo,
            self.template_combo,
            self.resource_combo,
            self.edit_template_button,
            self.asset_table,
            self.batch_checkbox,
            self.prompt_edit,
            self.project_edit,
            self.provider_combo,
            self.model_edit,
            self.base_url_edit,
            self.edit_plan_button,
            self.retry_button,
            self.regenerate_button,
            *self.header_controls,
        ]:
            control.setEnabled(not busy)
        self.scan_button.setEnabled(not busy)
        self.plan_button.setEnabled(not busy and self.scan_result is not None)
        self.build_button.setEnabled(
            not busy
            and (
                bool(self.current_preflight and self.current_preflight.ready)
                or any(item[2].ready for item in self.batch_builds)
            )
        )
        if message:
            self.status.setText(message)

    def _error(self, message: str) -> None:
        QMessageBox.critical(self, "DaVinciBot", message)
        self.status.setText(message)

    def edit_template(self):
        previous = self.database.get_template(self.template_combo.currentData())
        if not previous:
            self._error("Choose a template first.")
            return
        self.register_template(previous=previous)

    def edit_plan(self):
        if not self.current_job:
            return
        if not self.current_job.plan:
            dialog = JsonDialog("Edit preserved job inputs", self.current_job.spec.model_dump(mode="json"),
                                JobSpec.model_validate, self)
            if dialog.exec():
                spec = dialog.value.model_copy(update={"id": str(uuid4())})
                self.current_job = self.jobs.create(spec)
                self.status.setText("Inputs saved; use Retry with offline planner to replan.")
            return
        record = self.database.get_job(self.current_job.id)
        if record.state not in {JobState.PREFLIGHT, JobState.NEEDS_ATTENTION}:
            self._error("Regenerate approved jobs before editing their plans.")
            return

        def validate(value):
            spec = JobSpec.model_validate(value["job"])
            plan = EditPlan.model_validate(value["plan"])
            if (
                spec.id != record.id
                or plan.job_id != record.id
                or spec.profile_id != record.spec.profile_id
            ):
                raise ValueError("Job identity cannot be changed")
            return spec, plan

        dialog = JsonDialog(
            "Edit job assignments and plan",
            {
                "job": record.spec.model_dump(mode="json"),
                "plan": record.plan.model_dump(mode="json"),
            },
            validate,
            self,
        )
        if dialog.exec():
            record.spec, plan = dialog.value
            self.database.save_job(record)
            self.current_job = record
            self._plan_complete(plan)

    def retry_offline(self):
        if not self.current_job:
            self.provider_combo.setCurrentIndex(self.provider_combo.findData(ProviderKind.OFFLINE))
            self.create_plan()
            return
        spec = self.current_job.spec.model_copy(
            deep=True,
            update={
                "id": str(uuid4()),
                "provider": ProviderKind.OFFLINE,
                "provider_model": "offline-rules",
                "batch": False,
            },
        )
        chosen = self.database.get_template(self.template_combo.currentData())
        if chosen:
            spec.overrides.update(template_id=chosen.id, template_version=chosen.version)
        self.batch_builds = []
        self.current_preflight = None
        self.current_job = self.jobs.create(spec)
        self._busy(True, "Creating an offline plan from the preserved job inputs...")
        worker = Worker(
            self.planner.plan,
            spec,
            spec.profile_snapshot or self._profile(),
            self.database.list_templates(),
            make_provider,
        )
        worker.signals.succeeded.connect(self._plan_complete)
        worker.signals.failed.connect(self._plan_failed)
        worker.signals.finished.connect(lambda: self._busy(False))
        self.thread_pool.start(worker)

    def _selected_job_id(self):
        row = self.jobs_table.currentRow()
        item = self.jobs_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def cancel_selected(self):
        job_id = self._selected_job_id()
        if job_id:
            try:
                self.jobs.cancel(job_id)
                self._invalidate_plan()
                self.refresh_jobs()
            except Exception as error:
                self._error(str(error))

    def review_job(self):
        record = self.database.get_job(self._selected_job_id())
        if not record:
            self._error("Select a saved job.")
            return
        if not record.plan:
            self.current_job = record
            self.plan_view.setPlainText(record.spec.model_dump_json(indent=2))
            self.status.setText("Preserved inputs loaded; edit assignments or retry offline.")
            self._invalidate_plan()
            return
        if record.state in {
            JobState.QUEUED,
            JobState.ANALYZING,
            JobState.BUILDING,
            JobState.AWAITING_RESOLVE,
        }:
            self._error("Wait for the active build, or cancel it before regeneration.")
            return
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(record.spec.profile_id))
        if record.state in {
            JobState.READY,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.NEEDS_ATTENTION,
        }:
            if (
                QMessageBox.question(self, "Regenerate", "Create a new job using this saved plan?")
                != QMessageBox.StandardButton.Yes
            ):
                return
            spec = record.spec.model_copy(
                deep=True, update={"id": str(uuid4()), "prepared_audio": None, "batch": False}
            )
            plan = record.plan.model_copy(deep=True, update={"job_id": spec.id})
            record = self.jobs.create(spec)
            record = self.jobs.attach_plan(record.id, plan)
        self.batch_builds = []
        self.current_job = record
        self._plan_complete(record.plan)

    def manage_cache(self):
        if self.database.list_jobs([JobState.ANALYZING, JobState.BUILDING, JobState.QUEUED]):
            self._error("Wait for active jobs before managing the cache.")
            return
        cache = ManagedCache(self.paths.cache)
        limit, accepted = QInputDialog.getInt(
            self,
            "Derivative cache",
            f"Usage: {cache.usage_bytes() / 1024**3:.2f} GiB. "
            f"Pinned jobs: {len(cache.pins())}.\nKeep at most this many GiB of unpinned derivatives:",
            int(self.database.get_setting("cache_limit_gb", "20")),
            0,
            10000,
        )
        if not accepted:
            return
        self.database.set_setting("cache_limit_gb", str(limit))
        removed = cache.cleanup(limit * 1024**3)
        QMessageBox.information(
            self,
            "Cache cleanup",
            f"Deleted {len(removed)} unpinned derivative files. "
            "Sources and pinned master WAVs were preserved. "
            "Derivatives can be regenerated.",
        )

    def closeEvent(self, event):
        active = self.database.list_jobs([JobState.ANALYZING, JobState.BUILDING, JobState.QUEUED])
        if self._working or active:
            QMessageBox.information(
                self,
                "Work in progress",
                "Wait for active work to finish, or cancel queued analysis before closing.",
            )
            event.ignore()
            return
        self.poll_timer.stop()
        event.accept()
