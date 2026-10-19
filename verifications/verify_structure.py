# -*- coding: utf-8 -*-
"""Verify the project-structure engine and tab.

Run:  python verifications/verify_structure.py

It presses the buttons rather than looking for the calls: it parses every config
format, folds an inheritance chain, renders templates, refuses an escaping path,
generates a real tree on disk, validates it, runs the pipeline, packs a zip,
reverses a real folder back into a preset and migrates it -- then builds the tab
against a fake window and drives its actions.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5 import QtWidgets

from tabs import structure_core as core
from tabs.structure_tab import StructureTab

failures = []


def check(label, ok, detail=""):
    line = ("  OK   " if ok else "  FAIL ") + label + (
        "   <%s>" % detail if detail and not ok else "")
    print(str(line).encode("ascii", "replace").decode("ascii"))
    if not ok:
        failures.append(label)


class FakeWindow(QtWidgets.QWidget):
    def __init__(self, base):
        super().__init__()
        self.base_dir = base
        self.lang = "en"

    def notify_audit(self, event, **payload):
        return 1

    def t(self, key, fallback=None, **kwargs):
        text = fallback or key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text


print("configuration parser")
check("json parses", core.parse_config('{"a": 1, "b": [1, 2]}')["a"] == 1)
toml = core.parse_config('[project]\nname = "x"\nversion = 1\n')
check("toml parses a section", toml.get("project", {}).get("name") == "x")
yaml_text = ("name: demo\nfolders:\n  - src\n  - tests\ntokens:\n"
             "  genre: rpg\nbuild: pip\n")
yaml_data = core.parse_config(yaml_text)
check("yaml parses a scalar", yaml_data.get("name") == "demo")
check("yaml parses a block list",
      yaml_data.get("folders") == ["src", "tests"])
check("yaml parses a nested map",
      yaml_data.get("tokens", {}).get("genre") == "rpg")
check("a broken document returns {}", core.parse_config("{not json") == {})

print()
print("inheritance and overrides")
merged = core.resolve_inheritance(
    {"name": "child", "extends": "parent", "tokens": {"a": 2, "c": 3}},
    {"parent": {"tokens": {"a": 1, "b": 1}, "kind": "library"}})
check("a parent value is inherited", merged["tokens"]["b"] == 1)
check("the child overrides the parent", merged["tokens"]["a"] == 2)
check("a new child value survives", merged["tokens"]["c"] == 3)
check("the extends key is folded away", "extends" not in merged)
check("a non-dict parent value is inherited", merged["kind"] == "library")
cycle = core.resolve_inheritance({"name": "a", "extends": "b"},
                                 {"b": {"name": "b", "extends": "a"}})
check("a cycle terminates and yields a preset",
      isinstance(cycle, dict) and bool(cycle.get("name")))

print()
print("template engine")
text, missing = core.render_template("Hi ${name}, ${year}", {"name": "A",
                                                             "year": 2024})
check("plain tokens render", text == "Hi A, 2024" and not missing)
text, missing = core.render_template("x ${nope} y", {})
check("a missing token is reported", missing == ["nope"] and "${nope}" in text)
text, _m = core.render_template("a $select{x|y} b", {"y": "Y"})
check("a select picks the present option", text == "a Y b")
text, _m = core.render_template("$if{flag}ON$end", {"flag": True})
check("a true conditional renders", text == "ON")
text, _m = core.render_template("$if{flag}ON$end", {"flag": False})
check("a false conditional vanishes", text == "")
text, _m = core.render_template("$each{items}[${item}]$end",
                                {"items": ["a", "b"]})
check("a loop repeats the block", text == "[a][b]")

print()
print("path safety")
_path, error = core.safe_join("/tmp/project", "../evil")
check("an escaping path is refused", bool(error))
_path, error = core.safe_join("/tmp/project", "/abs/path")
check("an absolute path is refused", bool(error))
path, error = core.safe_join("/tmp/project", "src/main.py")
check("a normal path joins", not error and path.endswith("main.py"))
check("is_safe_relative rejects an escape",
      not core.is_safe_relative("../x"))
check("is_safe_relative accepts a nested path",
      core.is_safe_relative("src/pkg/mod.py"))

print()
print("generation and validation")
generator = core.StructureGenerator()
check("a built-in preset resolves",
      generator.resolve("python-app").get("name") == "python-app")
values, nodes = generator.build_tree(generator.resolve("python-app"), "demo")
files = [n for n in nodes if not n.folder]
folders = [n for n in nodes if n.folder]
check("the tree has files", len(files) >= 5)
check("the tree has folders", len(folders) >= 3)
check("the token is rendered in a path",
      any("test_demo" in n.path for n in files))
check("a file body is rendered",
      any("demo" in n.body for n in files if n.path == "README.md"))
report = core.validate_structure(nodes)
check("a built-in preset validates", report["ok"], str(report["errors"]))
check("the report counts files", report["files"] == len(files))
bad_nodes = [core.TreeNode("../escape.py", "x"),
             core.TreeNode("a.py", ""),
             core.TreeNode("dup.py", "1"), core.TreeNode("dup.py", "2")]
bad = core.validate_structure(bad_nodes)
kinds = {item["kind"] for item in bad["findings"]}
check("the validator flags an escape", "escape" in kinds)
check("the validator flags a conflict", "conflict" in kinds)
check("the validator flags an empty file", "empty" in kinds)
check("the summary text names the errors",
      "Structure validation" in core.format_validation(bad))

print()
with tempfile.TemporaryDirectory() as tmp:
    target = os.path.join(tmp, "demo")
    plan = core.plan_tree(nodes, target)
    actions = {item["action"] for item in plan}
    check("the dry-run plan lists creates", "create" in actions)
    check("the dry-run plan lists folders", "folder" in actions)
    check("the dry-run plan lists every tree node", len(plan) == len(nodes))
    check("the plan text mentions a dry run", "Dry run" in core.format_plan(
        plan))

    written = core.commit_tree(nodes, target)
    check("a commit writes the files", written["ok"] and written["files"] >= 5)
    check("the README is on disk",
          os.path.isfile(os.path.join(target, "README.md")))
    again = core.commit_tree(nodes, target)
    check("a second commit skips existing files",
          len(again["skipped"]) >= 1 and not again["errors"])

    print()
    print("pipeline, artifacts and presets")
    repository = core.PresetRepository(os.path.join(tmp, "presets"))
    check("the built-ins are in the repository",
          len(repository.names()) >= 9)
    zip_path = os.path.join(tmp, "demo.zip")
    result = core.run_pipeline(generator.resolve("python-app"), name="demo",
                               root=os.path.join(tmp, "demo2"), make_zip=True,
                               zip_target=zip_path, repository=repository)
    check("the pipeline completes", result["ok"] and
          result["stage"] == "complete", str(result.get("validation")))
    check("the zip artifact exists", os.path.isfile(zip_path))
    check("the zip has entries", result["artifacts"] and True)
    check("saving a preset bumps its version",
          repository.save_preset(dict(generator.resolve("python-app")),
                                 "user")["version"] >= 1)
    reloaded = core.PresetRepository(os.path.join(tmp, "presets"))
    check("a saved preset reloads",
          "python-app" in reloaded.names(scope="user"))
    check("a preset scope is reported", repository.scope_of("python-app") != "")
    check("preset versions are listed",
          len(repository.versions("python-app")) >= 1)

    print()
    print("reports")
    for kind in ("structure", "validation", "pipeline", "presets", "security"):
        text = core.structure_report(kind, result,
                                     generator.resolve("python-app"),
                                     repository)
        check("report %r is produced" % kind, len(text) > 40)

    print()
    print("reverse analysis and migration")
    outcome = core.analyze_project(target)
    check("a folder is analyzed", outcome["ok"] and outcome["files"] >= 5)
    check("the language is guessed", outcome["language"] == "python")
    check("the sample is listed", len(outcome["sample"]) >= 1)
    missing = core.suggest_missing(outcome["preset"], target)
    check("nothing is reported missing in a fresh tree", missing == [],
          str(missing))
    migrated = core.migrate_preset({"name": "old", "version": 1,
                                    "folders": ["src"], "files": []})
    check("migration adds the standard folders", "docs" in
          migrated["preset"]["folders"])
    check("migration is idempotent",
          core.migrate_preset(migrated["preset"])["changed"] == [])

    print()
    print("CLI and description")
    code, lines = core.cli(["validate", "python-app"])
    check("the CLI validates a preset", code == 0)
    code, lines = core.cli([])
    check("the CLI prints usage when empty", code == 2)
    parsed = core.parse_description("make an rpg game in godot")
    check("a sentence sets the kind", parsed["overrides"].get("kind") == "game")
    check("a sentence sets the language",
          parsed["overrides"].get("language") == "gdscript")
    check("a sentence infers a name", bool(parsed["name"]))

    print()
    print("plugins")
    plugins = core.PluginRegistry()
    plugins.register(core.__dict__ and __import__(
        "tabs.structure_tab", fromlist=["_LanguageEmitter"]).__dict__
        if False else object(), enabled=True)
    check("a nameless plugin is refused",
          not core.PluginRegistry().register(object())["ok"])

    print()
    print("the tab")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = FakeWindow(tmp)
    tab = StructureTab(window)
    check("the tab builds a generator", tab.generator is not None)
    check("the tab lists presets", tab.preset_combo.count() >= 9)
    tab.refresh()
    tab.preview()
    check("the tab previews a tree", tab.tree.topLevelItemCount() >= 1)
    tab.dry_run()
    check("the tab shows a dry-run plan", "Dry run" in tab.last_report)
    tab.validate()
    check("the tab validates", "Structure validation" in tab.last_report)
    tab.generate()
    check("the tab generates", tab.last_result.get("ok"))
    tab.compile_project()
    check("the tab compiles", tab.last_result.get("stage") == "complete")
    tab.load_builtin_plugins()
    check("the tab loads plugins", len(tab.plugins.summary()) >= 5)
    tab.plugin_table.selectRow(0)
    tab.toggle_plugin()
    tab.cli_edit.setText("validate python-app")
    tab.run_cli()
    check("the tab runs the CLI", "structure" in tab.last_report)
    tab.save_report()
    check("the tab saves a report", "Report written" in tab.status_label.text())
    tab.export_docs()
    check("the tab exports documentation",
          "Documentation written" in tab.status_label.text())
    tab.show_log()
    check("the tab shows the log", "Generation log" in tab.last_report)
    for kind, _lk, fallback, _d in core.PIPELINE_STAGES:
        pass
    tab.retranslate({key: "XX_" + key for key in (
        "st_title", "st_preview", "st_dry", "st_generate", "st_validate",
        "st_compile", "st_reverse", "st_describe", "st_overwrite", "st_browse",
        "st_panel_tree", "st_panel_edit", "st_panel_plugins",
        "st_panel_pipeline", "st_save_preset", "st_delete_preset",
        "st_export_presets", "st_import_presets", "st_plugin_load",
        "st_plugin_toggle", "st_save_report", "st_export_zip", "st_export_docs",
        "st_show_log", "st_cli_run")})
    check("retranslate reaches the title", tab.title_label.text() == "XX_st_title")
    check("retranslate reaches Generate",
          tab.btn_generate.text() == "XX_st_generate")
    check("retranslate reaches the panel",
          tab.panels.tabText(0) == "XX_st_panel_tree")
    check("retranslate reaches the CLI button",
          tab.btn_cli.text() == "XX_st_cli_run")

print()
print("%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
