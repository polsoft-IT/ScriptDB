# -*- coding: utf-8 -*-
"""Structure tab for ScriptDB ("Project structure").

This module owns the "Structure" tab: a mini build system for file trees.  It is
the interface to ``tabs.structure_core`` -- the engine turns a preset into a
tree, validates it and compiles it through a pipeline, and this tab only asks it
questions and shows the answers.

The tab follows the same one-file-per-tab pattern as every other tab:

    StructureTab(window)             -- build the tab
    tab.retranslate(table)           -- apply a language table (see TRANSLATIONS)
    tab.refresh()                    -- reload the preset list

What it offers, as the inner panels present it:

    presets    pick a preset (global / project / organization / user), see the
               chain it extends and the folders and files it declares, and edit
               or save it back
    tree       the folder tree the preset would produce, as a real tree view,
               with a dry run that shows create / overwrite / skip before
               anything is written
    templates  the live preview of a file's body with the tokens filled in
    plugins    load, enable and disable generators and hooks (hot-plug)
    pipeline   the build pipeline: generate, validate, post, artifacts, and the
               CI / IDE / Docker integrations
    CLI        the same engine through the command line, so the prompt and the
               window are never out of step

All of it is offline and dependency free: the module uses only the standard
library and never sends anything anywhere.

Wiring it into the main window::

    from tabs import StructureTab
    self.structure_tab = StructureTab(self)
    self.tabs.addTab(self.structure_tab, "Structure")
"""

import os

from PyQt5 import QtCore, QtWidgets

from . import structure_core as core


class StructureTab(QtWidgets.QWidget):
    """The project-structure workspace.

    The tab owns one :class:`tabs.structure_core.StructureGenerator` and one
    :class:`tabs.structure_core.PresetRepository`.  The tree it shows is always
    produced by the generator from the current preset, so the preview, the dry
    run and the real generation cannot disagree.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.directory = self._default_dir()
        self.generator = core.StructureGenerator()
        # The preset repository and the run log live *inside* the tab's own
        # workspace, never in the current working directory, so starting the
        # application (or a test) cannot litter the project folder.
        self.repository = core.PresetRepository(
            os.path.join(self.directory, core.PRESET_DIR))
        self.plugins = core.PluginRegistry()
        self.compiler = core.StructureCompiler(self.generator, self.plugins)
        self.log = core.StructureLog(
            os.path.join(self.directory, core.LOG_FILE))
        self.last_result = {}
        self.last_tree = []
        self.last_report = ""
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------- helpers
    def t(self, key, fallback=None, **kwargs):
        try:
            return self.window.t(key, fallback, **kwargs)
        except Exception:
            return fallback or key

    def _default_dir(self):
        base = (getattr(self.window, "base_dir", None)
                or getattr(self.window, "BASE_DIR", None)
                or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, core.STRUCTURE_DIR)

    def target_dir(self):
        text = self.target_edit.text().strip()
        if text:
            return os.path.abspath(text)
        return os.path.join(self.directory, self._current_name() or "project")

    def _current_name(self):
        """The preset name behind the current picker row."""
        data = self.preset_combo.currentData()
        return (data or "").strip()

    def _current_preset(self):
        """The preset the picker shows, with its inheritance already folded in."""
        name = self._current_name()
        if not name:
            return {}
        return self.repository.get(name) or self.generator.resolve(name)

    def _status(self, message):
        self.status_label.setText(message)
        return message

    def _announce(self, detail):
        """Record a structure action on the shared audit timeline."""
        try:
            self.log.record("st_compiled", detail)
        except Exception:
            pass
        audit = getattr(self.window, "audit_tab", None)
        if audit is not None and hasattr(audit, "log_change"):
            try:
                audit.log_change("modules", "structure", detail, "structure")
                return
            except Exception:
                pass
        notify = getattr(self.window, "notify_audit", None)
        if callable(notify):
            try:
                notify("module_updated", where="structure", detail=detail)
            except Exception:
                pass

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # ---- header -------------------------------------------------------
        header = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(self.t("st_title", "STRUCTURE"))
        self.title_label.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #8ab4f8;")
        header.addWidget(self.title_label)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label)
        header.addStretch()
        self.state_label = QtWidgets.QLabel("")
        self.state_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.state_label)
        layout.addLayout(header)

        # ---- preset / target row ------------------------------------------
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.addWidget(QtWidgets.QLabel(self.t("st_preset", "Preset:")))
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.setMinimumWidth(220)
        self.preset_combo.currentIndexChanged.connect(self.on_preset_changed)
        preset_row.addWidget(self.preset_combo)
        preset_row.addWidget(QtWidgets.QLabel(self.t("st_name", "Project:")))
        self.name_edit = QtWidgets.QLineEdit(self.t("st_default_name",
                                                    "project"))
        self.name_edit.setMaximumWidth(160)
        self.name_edit.editingFinished.connect(self.preview)
        preset_row.addWidget(self.name_edit)
        preset_row.addWidget(QtWidgets.QLabel(self.t("st_target", "Target:")))
        self.target_edit = QtWidgets.QLineEdit("")
        self.target_edit.setPlaceholderText(self.t("st_target_hint",
                                                   "leave empty for the "
                                                   "workspace"))
        preset_row.addWidget(self.target_edit)
        self.btn_browse = QtWidgets.QPushButton(self.t("st_browse", "Browse..."))
        self.btn_browse.clicked.connect(self.browse_target)
        preset_row.addWidget(self.btn_browse)
        layout.addLayout(preset_row)

        # ---- action row ---------------------------------------------------
        action_row = QtWidgets.QHBoxLayout()
        for name, label_key, fallback, slot in (
                ("btn_preview", "st_preview", "Preview", self.preview),
                ("btn_dry_run", "st_dry", "Dry run", self.dry_run),
                ("btn_generate", "st_generate", "Generate", self.generate),
                ("btn_validate", "st_validate", "Validate", self.validate),
                ("btn_compile", "st_compile", "Compile", self.compile_project),
                ("btn_reverse", "st_reverse", "Analyze folder...",
                 self.reverse_project),
                ("btn_description", "st_describe", "From a sentence...",
                 self.generate_from_description)):
            button = QtWidgets.QPushButton(self.t(label_key, fallback))
            button.clicked.connect(slot)
            setattr(self, name, button)
            action_row.addWidget(button)
        action_row.addStretch()
        self.overwrite_check = QtWidgets.QCheckBox(
            self.t("st_overwrite", "Overwrite"))
        action_row.addWidget(self.overwrite_check)
        layout.addLayout(action_row)

        # ---- inner panels -------------------------------------------------
        self.panels = QtWidgets.QTabWidget()
        self.panels.addTab(self._build_tree_panel(), self.t("st_panel_tree",
                                                            "Tree"))
        self.panels.addTab(self._build_edit_panel(), self.t("st_panel_edit",
                                                            "Preset"))
        self.panels.addTab(self._build_plugins_panel(),
                           self.t("st_panel_plugins", "Plugins"))
        self.panels.addTab(self._build_pipeline_panel(),
                           self.t("st_panel_pipeline", "Pipeline"))
        layout.addWidget(self.panels)

        # ---- report / log -------------------------------------------------
        self.report_view = QtWidgets.QPlainTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setPlaceholderText(
            self.t("st_report_hint", "A tree, a plan, a report or CLI output "
                                     "appears here."))
        self.report_view.setMaximumHeight(220)
        layout.addWidget(self.report_view)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setStyleSheet("color: #9aa0a6;")
        layout.addWidget(self.status_label)

    def _build_tree_panel(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels([self.t("st_col_path", "Path"),
                                   self.t("st_col_kind", "Kind"),
                                   self.t("st_col_size", "Size")])
        self.tree.setColumnWidth(0, 420)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection)
        layout.addWidget(self.tree)
        return widget

    def _build_edit_panel(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        form = QtWidgets.QFormLayout()
        self.edit_title = QtWidgets.QLineEdit()
        self.edit_kind = QtWidgets.QComboBox()
        for key, _lk, fallback, _d in core.STRUCTURE_KINDS:
            self.edit_kind.addItem(self.t("st_kind_" + key, fallback), key)
        self.edit_language = QtWidgets.QComboBox()
        for key, _lk, fallback, _e, _n in core.STRUCTURE_LANGUAGES:
            self.edit_language.addItem(self.t("st_lang_" + key, fallback), key)
        self.edit_build = QtWidgets.QComboBox()
        for key, _lk, fallback, _files in core.BUILD_SYSTEMS:
            self.edit_build.addItem(self.t("st_build_" + key, fallback), key)
        self.edit_ci = QtWidgets.QComboBox()
        for key, _lk, fallback in core.CI_SYSTEMS:
            self.edit_ci.addItem(self.t("st_ci_" + key, fallback), key)
        self.edit_ide = QtWidgets.QComboBox()
        for key, _lk, fallback in core.IDE_TARGETS:
            self.edit_ide.addItem(self.t("st_ide_" + key, fallback), key)
        self.edit_extends = QtWidgets.QLineEdit()
        self.edit_extends.setPlaceholderText(self.t("st_extends_hint",
                                                    "extends a preset name"))
        form.addRow(self.t("st_edit_title", "Title"), self.edit_title)
        form.addRow(self.t("st_edit_kind", "Kind"), self.edit_kind)
        form.addRow(self.t("st_edit_language", "Language"), self.edit_language)
        form.addRow(self.t("st_edit_build", "Build system"), self.edit_build)
        form.addRow(self.t("st_edit_ci", "CI"), self.edit_ci)
        form.addRow(self.t("st_edit_ide", "IDE"), self.edit_ide)
        form.addRow(self.t("st_edit_extends", "Extends"), self.edit_extends)
        layout.addLayout(form)

        template_row = QtWidgets.QHBoxLayout()
        self.template_combo = QtWidgets.QComboBox()
        self.template_combo.currentIndexChanged.connect(self.on_template_changed)
        template_row.addWidget(QtWidgets.QLabel(self.t("st_template",
                                                       "Template:")))
        template_row.addWidget(self.template_combo)
        template_row.addStretch()
        layout.addLayout(template_row)
        self.template_view = QtWidgets.QPlainTextEdit()
        self.template_view.setReadOnly(True)
        layout.addWidget(self.template_view)

        button_row = QtWidgets.QHBoxLayout()
        self.btn_save_preset = QtWidgets.QPushButton(
            self.t("st_save_preset", "Save preset"))
        self.btn_save_preset.clicked.connect(self.save_preset)
        self.btn_delete_preset = QtWidgets.QPushButton(
            self.t("st_delete_preset", "Delete preset"))
        self.btn_delete_preset.clicked.connect(self.delete_preset)
        self.btn_export_presets = QtWidgets.QPushButton(
            self.t("st_export_presets", "Export presets"))
        self.btn_export_presets.clicked.connect(self.export_presets)
        self.btn_import_presets = QtWidgets.QPushButton(
            self.t("st_import_presets", "Import presets"))
        self.btn_import_presets.clicked.connect(self.import_presets)
        for button in (self.btn_save_preset, self.btn_delete_preset,
                       self.btn_export_presets, self.btn_import_presets):
            button_row.addWidget(button)
        button_row.addStretch()
        layout.addLayout(button_row)
        return widget

    def _build_plugins_panel(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self.plugin_table = QtWidgets.QTableWidget(0, 2)
        self.plugin_table.setHorizontalHeaderLabels([
            self.t("st_col_plugin", "Plugin"),
            self.t("st_col_state", "State")])
        self.plugin_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.plugin_table)
        row = QtWidgets.QHBoxLayout()
        self.btn_plugin_load = QtWidgets.QPushButton(
            self.t("st_plugin_load", "Load built-in generators"))
        self.btn_plugin_load.clicked.connect(self.load_builtin_plugins)
        self.btn_plugin_toggle = QtWidgets.QPushButton(
            self.t("st_plugin_toggle", "Enable / disable"))
        self.btn_plugin_toggle.clicked.connect(self.toggle_plugin)
        row.addWidget(self.btn_plugin_load)
        row.addWidget(self.btn_plugin_toggle)
        row.addStretch()
        layout.addLayout(row)
        return widget

    def _build_pipeline_panel(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        self.stage_list = QtWidgets.QListWidget()
        for key, _lk, fallback, detail in core.PIPELINE_STAGES:
            item = QtWidgets.QListWidgetItem("%s  -  %s" % (fallback, detail))
            item.setData(QtCore.Qt.UserRole, key)
            self.stage_list.addItem(item)
        layout.addWidget(self.stage_list)
        row = QtWidgets.QHBoxLayout()
        self.btn_save_report = QtWidgets.QPushButton(
            self.t("st_save_report", "Save report"))
        self.btn_save_report.clicked.connect(self.save_report)
        self.btn_export_zip = QtWidgets.QPushButton(
            self.t("st_export_zip", "Export zip"))
        self.btn_export_zip.clicked.connect(self.export_zip)
        self.btn_export_docs = QtWidgets.QPushButton(
            self.t("st_export_docs", "Export docs"))
        self.btn_export_docs.clicked.connect(self.export_docs)
        self.btn_show_log = QtWidgets.QPushButton(self.t("st_show_log", "Log"))
        self.btn_show_log.clicked.connect(self.show_log)
        for button in (self.btn_save_report, self.btn_export_zip,
                       self.btn_export_docs, self.btn_show_log):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        cli_row = QtWidgets.QHBoxLayout()
        self.cli_edit = QtWidgets.QLineEdit("")
        self.cli_edit.setPlaceholderText(self.t("st_cli_hint",
                                                "validate python-app"))
        cli_row.addWidget(self.cli_edit)
        self.btn_cli = QtWidgets.QPushButton(self.t("st_cli_run", "Run CLI"))
        self.btn_cli.clicked.connect(self.run_cli)
        cli_row.addWidget(self.btn_cli)
        layout.addLayout(cli_row)
        # The panel needs a few buttons the main action row owns; keep a
        # reference so retranslate can reach them.
        return widget
    # ---------------------------------------------------------- the pickers
    def refresh(self):
        """Reload the preset list and refresh the tree."""
        self.repository.load()
        current = self._current_name()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for name in self.repository.names():
            label = "%s  [%s]" % (name, self.repository.scope_of(name))
            self.preset_combo.addItem(label, name)
        self.preset_combo.blockSignals(False)
        if current:
            self.select_preset(current)
        self.count_label.setText(self.t("st_preset_count", "{0} preset(s)")
                                 .format(len(self.repository.names())))
        self.refresh_plugins()
        self.on_preset_changed()

    def select_preset(self, name):
        for index in range(self.preset_combo.count()):
            if self.preset_combo.itemData(index) == name:
                self.preset_combo.setCurrentIndex(index)
                return True
        return False

    def on_preset_changed(self, *_args):
        """Fill the editor from the preset the picker now shows."""
        preset = self._current_preset()
        self.edit_title.setText(preset.get("title", ""))
        self._select_data(self.edit_kind, preset.get("kind", "application"))
        self._select_data(self.edit_language, preset.get("language", "python"))
        self._select_data(self.edit_build, preset.get("build", "none"))
        self._select_data(self.edit_ci, preset.get("ci", "none"))
        self._select_data(self.edit_ide, preset.get("ide", "none"))
        self.edit_extends.setText(preset.get("extends", ""))
        self.fill_template_combo(preset)
        self.state_label.setText(
            self.t("st_state_scope", "scope: {0}  v{1}").format(
                self.repository.scope_of(self._current_name()),
                preset.get("version", 1)))
        self.preview()

    @staticmethod
    def _select_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def fill_template_combo(self, preset):
        """List the files of the preset so one can be previewed."""
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for path, body in preset.get("files", []):
            self.template_combo.addItem(path, body)
        self.template_combo.blockSignals(False)
        self.on_template_changed()

    # ------------------------------------------------------------- previews
    def preview(self):
        """Rebuild the tree from the current preset and show it."""
        preset = self._current_preset()
        if not preset:
            return self._status(self.t("st_no_preset", "No preset selected."))
        try:
            values, nodes = self.generator.build_tree(
                preset, self.name_edit.text().strip() or preset.get("name", ""))
        except Exception as exc:
            return self._status(str(exc))
        self.last_tree = nodes
        self.last_result = {"tree": nodes, "preset": preset.get("name")}
        self.fill_tree(nodes)
        self.fill_template_preview(values)
        files = sum(1 for node in nodes if not node.folder)
        folders = sum(1 for node in nodes if node.folder)
        self._status(self.t("st_previewed", "{0} file(s), {1} folder(s).")
                     .format(files, folders))
        return nodes

    def fill_tree(self, nodes):
        self.tree.clear()
        items = {}
        for node in nodes:
            parent_path = os.path.dirname(node.path).replace(os.sep, "/")
            parent = items.get(parent_path) if parent_path else None
            label = node.path.split("/")[-1] + ("/" if node.folder else "")
            # A QTreeWidgetItem with no parent is not added to the tree on its
            # own -- it must be handed to addTopLevelItem, which is what left
            # the view empty until this was fixed.
            item = (QtWidgets.QTreeWidgetItem(parent) if parent is not None
                    else QtWidgets.QTreeWidgetItem(self.tree))
            item.setText(0, label)
            item.setText(1, self.t("st_folder", "folder") if node.folder
                          else self.t("st_file", "file"))
            item.setText(2, "" if node.folder else "%d B"
                          % len(node.body or ""))
            item.setData(0, QtCore.Qt.UserRole, node.path)
            if not node.folder and node.missing:
                item.setForeground(0, QtCore.Qt.yellow)
                item.setToolTip(0, self.t("st_unfilled", "unfilled token(s): ")
                                + ", ".join(node.missing))
            items[node.path] = item
        self.tree.expandAll()

    def fill_template_preview(self, values):
        index = self.template_combo.currentIndex()
        if index < 0:
            self.template_view.setPlainText("")
            return
        body = self.template_combo.itemData(index) or ""
        rendered, missing = core.render_template(body, values)
        header = ""
        if missing:
            header = (self.t("st_unfilled", "unfilled token(s): ")
                      + ", ".join(sorted(set(missing))) + "\n\n")
        self.template_view.setPlainText(header + rendered)

    def on_template_changed(self, *_args):
        preset = self._current_preset()
        values = self.generator.values_for(
            preset, self.name_edit.text().strip() or preset.get("name", ""))
        self.fill_template_preview(values)

    def on_tree_selection(self):
        items = self.tree.selectedItems()
        if not items:
            return
        path = items[0].data(0, QtCore.Qt.UserRole)
        for node in self.last_tree:
            if node.path == path and not node.folder:
                self.template_view.setPlainText(node.body or "")
                return

    # ------------------------------------------------------------- actions
    def browse_target(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("st_browse", "Browse..."), self.directory)
        if directory:
            self.target_edit.setText(directory)

    def dry_run(self):
        """Show exactly what a generation would create, overwrite or skip."""
        preset = self._current_preset()
        if not preset:
            return self._status(self.t("st_no_preset", "No preset selected."))
        values, nodes = self.generator.build_tree(
            preset, self.name_edit.text().strip() or preset.get("name", ""))
        plan = core.plan_tree(nodes, self.target_dir())
        self.last_tree = nodes
        self.fill_tree(nodes)
        self._report(core.format_plan(plan))
        self._status(self.t("st_dry_done",
                            "Dry run: {0} action(s).").format(len(plan)))
        self._announce("dry run of %s" % preset.get("name"))
        return plan

    def generate(self):
        """Write the tree to the target directory."""
        preset = self._current_preset()
        if not preset:
            return self._status(self.t("st_no_preset", "No preset selected."))
        target = self.target_dir()
        result = self.compiler.compile(
            preset, name=self.name_edit.text().strip() or preset.get("name", ""),
            root=target, overwrite=self.overwrite_check.isChecked())
        self.last_result = result
        self.last_tree = result.get("tree", [])
        self.fill_tree(self.last_tree)
        self._report(self._format_result(result))
        if result.get("ok"):
            self._status(self.t("st_generated", "Generated into {0}.")
                         .format(target))
            self.log.record("st_generated", "wrote into %s" % target,
                            files=(result.get("commit") or {}).get("files", 0))
            self._announce("generated %s" % preset.get("name"))
        else:
            self._status(self.t("st_generate_failed",
                                "Generation stopped at: {0}")
                         .format(result.get("stage", "")))
        return result

    def validate(self):
        """Validate the tree without writing anything."""
        preset = self._current_preset()
        if not preset:
            return self._status(self.t("st_no_preset", "No preset selected."))
        _values, nodes = self.generator.build_tree(
            preset, self.name_edit.text().strip() or preset.get("name", ""))
        report = core.validate_structure(nodes, self.target_dir())
        self.last_tree = nodes
        self.fill_tree(nodes)
        self.last_result = {"tree": nodes, "validation": report}
        self._report(core.format_validation(report))
        self._status(self.t("st_validated", "Valid: {0}  errors: {1}  "
                            "warnings: {2}").format(
            report["ok"], len(report["errors"]), len(report["warnings"])))
        self.log.record("st_validated", "checked %s" % preset.get("name"),
                        errors=len(report["errors"]))
        return report

    def compile_project(self):
        """Run the whole pipeline, including the artifacts stage."""
        preset = self._current_preset()
        if not preset:
            return self._status(self.t("st_no_preset", "No preset selected."))
        target = self.target_dir()
        result = core.run_pipeline(
            preset, name=self.name_edit.text().strip() or preset.get("name", ""),
            root=target, overwrite=self.overwrite_check.isChecked(),
            make_zip=True,
            zip_target=os.path.join(self.directory, core.EXPORT_DIR,
                                    "%s.zip" % core._slug(preset.get("name",
                                                                     "project"))),
            repository=self.repository)
        self.last_result = result
        self.last_tree = result.get("tree", [])
        self.fill_tree(self.last_tree)
        self._report(self._format_result(result))
        self._status(self.t("st_compiled", "Compiled: {0}  artifacts: {1}")
                     .format(result.get("stage"), len(result.get("artifacts",
                                                                 []))))
        self.log.record("st_compiled", "compiled %s" % preset.get("name"),
                        stage=result.get("stage"))
        self._announce("compiled %s" % preset.get("name"))
        return result

    def reverse_project(self):
        """Read an existing folder back into a preset (reverse engineering)."""
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, self.t("st_reverse", "Analyze folder..."), self.directory)
        if not directory:
            return None
        outcome = core.analyze_project(directory)
        if not outcome["ok"]:
            return self._status(outcome["detail"])
        preset = outcome["preset"]
        self.repository.save_preset(preset, "user")
        self.refresh()
        self.select_preset(preset["name"])
        self._report("\n".join([
            self.t("st_reversed", "Analyzed {0}").format(directory),
            "  folders: %d" % outcome["folders"],
            "  files:   %d" % outcome["files"],
            "  language: %s" % outcome["language"],
            "",
            self.t("st_reverse_sample", "Sample"),
        ] + ["  " + path for path in outcome["sample"]]))
        self._status(self.t("st_reversed_status",
                            "Read {0} file(s) into preset {1}.").format(
            outcome["files"], preset["name"]))
        self.log.record("st_reversed", directory, files=outcome["files"])
        return outcome

    def generate_from_description(self):
        """Turn a sentence into a preset and generate from it."""
        text, ok = QtWidgets.QInputDialog.getText(
            self, self.t("st_describe", "From a sentence..."),
            self.t("st_describe_hint",
                   "Describe the structure, e.g. \"an rpg game in godot\":"))
        if not ok or not text.strip():
            return None
        base = self._current_preset() or {"kind": "application",
                                          "language": "python"}
        applied = core.apply_description(base, text)
        if not applied["ok"]:
            return self._status(applied["detail"])
        preset = applied["preset"]
        preset["name"] = preset.get("name") or "custom"
        self.repository.save_preset(preset, "user")
        self.refresh()
        self.select_preset(preset["name"])
        self.name_edit.setText(applied["name"])
        self._status(self.t("st_described",
                            "Understood: {0}").format(
            ", ".join(applied["matched"]) or self.t("st_nothing", "nothing")))
        self.preview()
        return preset

    # ----------------------------------------------------------- presets
    def save_preset(self):
        """Save the editor values as a user preset."""
        preset = dict(self._current_preset())
        preset["title"] = self.edit_title.text().strip() or preset.get("name")
        preset["kind"] = self.edit_kind.currentData()
        preset["language"] = self.edit_language.currentData()
        preset["build"] = self.edit_build.currentData()
        preset["ci"] = self.edit_ci.currentData()
        preset["ide"] = self.edit_ide.currentData()
        extends = self.edit_extends.text().strip()
        if extends:
            preset["extends"] = extends
        name = self._current_name()
        if not name:
            return self._status(self.t("st_no_preset", "No preset selected."))
        preset["name"] = name
        outcome = self.repository.save_preset(preset, "user")
        self.refresh()
        self._status(self.t("st_saved", "Preset saved: {0} v{1}").format(
            outcome.get("name", name), outcome.get("version", 1)))
        self.log.record("st_preset_saved", name)
        return outcome

    def delete_preset(self):
        name = self._current_name()
        if not name:
            return self._status(self.t("st_no_preset", "No preset selected."))
        outcome = self.repository.delete_preset(name)
        self.refresh()
        self._status(self.t("st_deleted", "Preset deleted: {0}").format(name))
        self.log.record("st_preset_deleted", name)
        return outcome

    def export_presets(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, self.t("st_export_presets", "Export presets"),
            os.path.join(self.directory, "presets.json"), "JSON (*.json)")
        if not path:
            return None
        outcome = core.export_preset_pack(self.repository.all(), path)
        self._status(self.t("st_exported_presets",
                            "Preset pack written: {0}").format(path))
        self.log.record("st_exported", path)
        return outcome

    def import_presets(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, self.t("st_import_presets", "Import presets"), self.directory,
            "JSON (*.json)")
        if not path:
            return None
        outcome = core.import_preset_pack(path)
        if not outcome["ok"]:
            return self._status(outcome["detail"])
        count = 0
        for scope, bucket in (outcome["presets"] or {}).items():
            for _name, preset in (bucket or {}).items():
                if isinstance(preset, dict):
                    self.repository.save_preset(preset,
                                                scope if scope in
                                                core.PRESET_SCOPE_KEYS
                                                else "user")
                    count += 1
        self.refresh()
        self._status(self.t("st_imported_presets",
                            "Preset pack read: {0} preset(s)").format(count))
        self.log.record("st_imported", path, count=count)
        return outcome

    # ----------------------------------------------------------- plugins
    def refresh_plugins(self):
        self.plugin_table.setRowCount(0)
        for entry in self.plugins.summary():
            row = self.plugin_table.rowCount()
            self.plugin_table.insertRow(row)
            self.plugin_table.setItem(row, 0,
                                      QtWidgets.QTableWidgetItem(entry["name"]))
            self.plugin_table.setItem(row, 1, QtWidgets.QTableWidgetItem(
                self.t("st_enabled", "enabled") if entry["enabled"]
                else self.t("st_disabled", "disabled")))

    def load_builtin_plugins(self):
        """Register the built-in emitters as plugins (a demonstration of hot-plug)."""
        self.plugins.register(_LanguageEmitter("python", "python"))
        self.plugins.register(_LanguageEmitter("node", "node"))
        self.plugins.register(_LanguageEmitter("cpp", "cpp"))
        self.plugins.register(_BuildEmitter("cmake"))
        self.plugins.register(_BuildEmitter("npm"))
        self.refresh_plugins()
        self._status(self.t("st_plugins_loaded", "Built-in generators loaded."))
        self.log.record("st_plugin_loaded", "built-in generators")
        return self.plugins.summary()

    def toggle_plugin(self):
        row = self.plugin_table.currentRow()
        if row < 0:
            return self._status(self.t("st_pick_plugin", "Pick a plugin first."))
        name = self.plugin_table.item(row, 0).text()
        state = not self.plugins.enabled.get(name, False)
        self.plugins.enabled[name] = state
        self.refresh_plugins()
        self._status(self.t("st_plugin_toggled", "{0}: {1}").format(
            name, self.t("st_enabled", "enabled") if state
            else self.t("st_disabled", "disabled")))
        return state

    # --------------------------------------------------------- pipeline/CLI
    def run_cli(self):
        """Run the engine through its command-line front end."""
        text = self.cli_edit.text().strip()
        if not text:
            return self._status(self.t("st_cli_empty", "Type a CLI command."))
        code, lines = core.cli(text.split())
        self._report("\n".join(["$ structure " + text, ""] + list(lines)))
        self._status(self.t("st_cli_done", "CLI exit code: {0}").format(code))
        self.log.record("st_compiled", "cli: %s" % text, code=code)
        return code, lines

    def save_report(self):
        kind = "structure"
        directory = os.path.join(self.directory, core.EXPORT_DIR)
        os.makedirs(directory, exist_ok=True)
        text = core.structure_report(kind, self.last_result,
                                     self._current_preset(), self.repository)
        path = os.path.join(directory, "structure-%s.txt" % core._stamp())
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self._status(self.t("st_report_saved", "Report written: {0}").format(
            os.path.basename(path)))
        self.log.record("st_exported", path)
        return path

    def export_zip(self):
        """Pack the current target directory into a zip."""
        target = self.target_dir()
        if not os.path.isdir(target):
            return self._status(self.t("st_no_target",
                                       "No generated target to pack."))
        paths = [os.path.relpath(os.path.join(base, name), target)
                 .replace(os.sep, "/")
                 for base, _dirs, names in os.walk(target) for name in names]
        out = os.path.join(self.directory, core.EXPORT_DIR,
                           "%s.zip" % core._slug(os.path.basename(target)))
        outcome = core.write_zip(target, paths, out)
        self._status(self.t("st_zip_written", "Zip written: {0} ({1} file(s))")
                     .format(os.path.basename(out), outcome.get("files", 0)))
        self.log.record("st_exported", out)
        return outcome

    def export_docs(self):
        if not self.last_tree:
            return self._status(self.t("st_no_tree", "Preview a tree first."))
        out = os.path.join(self.directory, core.EXPORT_DIR, "STRUCTURE.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        outcome = core.export_documentation(self.last_tree, out,
                                            self._current_name() or "Project")
        self._status(self.t("st_docs_written", "Documentation written: {0}")
                     .format(out))
        return outcome

    def show_log(self):
        rows = self.log.rows()
        text = [self.t("st_log", "Generation log"), ""]
        for time, event, detail in rows:
            text.append("  %s  %-20s %s" % (time, event, detail))
        if not rows:
            text.append("  " + self.t("st_log_empty", "nothing recorded yet"))
        self._report("\n".join(text))
        return rows

    def _report(self, text):
        self.last_report = text
        self.report_view.setPlainText(text)

    def _format_result(self, result):
        lines = ["%s: %s" % (self.t("st_preset", "Preset"),
                             result.get("preset", "")),
                 "%s: %s" % (self.t("st_stage", "Stage"),
                             result.get("stage", "")),
                 "%s: %d" % (self.t("st_col_size", "Size"),
                             len(result.get("tree", [])))]
        validation = result.get("validation") or {}
        if validation:
            lines.append("")
            lines.append(core.format_validation(validation))
        commit = result.get("commit") or {}
        if commit:
            lines += ["", "%s: %d" % (self.t("st_written", "Written"),
                                      commit.get("files", 0)),
                      "%s: %d" % (self.t("st_skipped", "Skipped"),
                                  len(commit.get("skipped", [])))]
            for error in commit.get("errors", []):
                lines.append("  ! %s: %s" % (error.get("path"),
                                             error.get("detail")))
        if result.get("plan"):
            lines += ["", core.format_plan(result["plan"])]
        if result.get("artifacts"):
            lines += ["", "%s:" % self.t("st_artifacts", "Artifacts")]
            lines += ["  " + path for path in result["artifacts"]]
        return "\n".join(lines)

    # ------------------------------------------------------------ translation
    def retranslate(self, table):
        """Apply the main window's language table to this tab."""
        def label(key, fallback):
            return table.get(key, fallback)

        self.title_label.setText(label("st_title", "STRUCTURE"))
        self.btn_preview.setText(label("st_preview", "Preview"))
        self.btn_dry_run.setText(label("st_dry", "Dry run"))
        self.btn_generate.setText(label("st_generate", "Generate"))
        self.btn_validate.setText(label("st_validate", "Validate"))
        self.btn_compile.setText(label("st_compile", "Compile"))
        self.btn_reverse.setText(label("st_reverse", "Analyze folder..."))
        self.btn_description.setText(label("st_describe", "From a sentence..."))
        self.overwrite_check.setText(label("st_overwrite", "Overwrite"))
        self.btn_browse.setText(label("st_browse", "Browse..."))
        self.panels.setTabText(0, label("st_panel_tree", "Tree"))
        self.panels.setTabText(1, label("st_panel_edit", "Preset"))
        self.panels.setTabText(2, label("st_panel_plugins", "Plugins"))
        self.panels.setTabText(3, label("st_panel_pipeline", "Pipeline"))
        self.tree.setHeaderLabels([label("st_col_path", "Path"),
                                   label("st_col_kind", "Kind"),
                                   label("st_col_size", "Size")])
        self.plugin_table.setHorizontalHeaderLabels([
            label("st_col_plugin", "Plugin"), label("st_col_state", "State")])
        self.report_view.setPlaceholderText(
            label("st_report_hint", "A tree, a plan, a report or CLI output "
                                    "appears here."))
        self.btn_save_preset.setText(label("st_save_preset", "Save preset"))
        self.btn_delete_preset.setText(label("st_delete_preset",
                                             "Delete preset"))
        self.btn_export_presets.setText(label("st_export_presets",
                                              "Export presets"))
        self.btn_import_presets.setText(label("st_import_presets",
                                              "Import presets"))
        self.btn_plugin_load.setText(label("st_plugin_load",
                                           "Load built-in generators"))
        self.btn_plugin_toggle.setText(label("st_plugin_toggle",
                                             "Enable / disable"))
        self.btn_save_report.setText(label("st_save_report", "Save report"))
        self.btn_export_zip.setText(label("st_export_zip", "Export zip"))
        self.btn_export_docs.setText(label("st_export_docs", "Export docs"))
        self.btn_show_log.setText(label("st_show_log", "Log"))
        self.btn_cli.setText(label("st_cli_run", "Run CLI"))
        self.cli_edit.setPlaceholderText(label("st_cli_hint",
                                               "validate python-app"))
        self.target_edit.setPlaceholderText(
            label("st_target_hint", "leave empty for the workspace"))
        self._retranslate_combos()
        self.refresh()

    def _retranslate_combos(self):
        """Relabel every combo that is built from an engine table."""
        for combo, prefix in ((self.edit_kind, "st_kind_"),
                              (self.edit_language, "st_lang_"),
                              (self.edit_build, "st_build_"),
                              (self.edit_ci, "st_ci_"),
                              (self.edit_ide, "st_ide_")):
            for index in range(combo.count()):
                key = combo.itemData(index)
                combo.setItemText(index,
                                  self.t(prefix + key, combo.itemText(index)))


# ---------------------------------------------------------------------------
# Built-in plugins (loaded on demand from the Plugins panel)
# ---------------------------------------------------------------------------
class _LanguageEmitter(object):
    """A plugin that adds a language-specific file to a preset."""

    def __init__(self, name, language):
        self.name = name
        self.language = language

    def generate(self, preset, values):
        ext = core.STRUCTURE_LANGUAGE_EXT.get(self.language, ".py")
        stem = core._slug(values.get("slug", "module"))
        return [("src/%s_%s%s" % (stem, self.language, ext),
                 core.module_body(self.language, values.get("project", "m")))]


class _BuildEmitter(object):
    """A plugin that adds a build-system file to a preset."""

    def __init__(self, build):
        self.name = "build-" + build
        self.build = build

    def generate(self, preset, values):
        return core.build_files(self.build, values.get("language", "python"),
                                values)
