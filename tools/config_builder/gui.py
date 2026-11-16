"""Tkinter forms; all validation and writes go through model.py."""
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import model

COPY = {
    "roots": ("許可フォルダ", "Allowed folders"),
    "language": ("承認窓の言語", "Window language"),
    "tasks": ("タスクプリセット", "Task presets"),
    "review": ("確認と保存", "Review and save"),
    "warning": ("許可したフォルダ内のファイルは、承認窓なしで ChatGPT から読まれます。秘密情報を含むフォルダは許可しないでください。",
                "ChatGPT can read files in allowed folders without an approval window. Do not allow folders containing secrets."),
    "read_limit": ("読み取り専用はファイル操作ツールの制限です。承認したコマンドによる書き込みは防げません。",
                   "Read-only limits file tools. It cannot prevent writes by an approved command."),
    "roots_enabled": ("許可フォルダ設定を保存する（オフならcheckoutのみ）", "Save allowed folders (off uses checkout only)"),
    "tasks_enabled": ("タスク設定を保存する（オフならタスクなし）", "Save task settings (off means no tasks)"),
    "add": ("フォルダを追加（読むだけ）", "Add folder (read-only)"),
    "read": ("読むだけにする", "Set read-only"),
    "full": ("フルにする", "Set full access"),
    "remove": ("選択項目を削除", "Remove selected"),
    "full_warning": ("フルアクセスにすると、書き込み先とコマンドの作業場所として許可されます。変更しますか？",
                     "Full access permits this folder for writes and command working directories. Continue?"),
    "system": ("未設定（Windowsに合わせる）", "Unset (follow Windows)"),
    "apply": ("フォームをJSONに反映", "Apply form to JSON"),
    "new": ("新しいフォーム", "New form"),
    "reset": ("JSONを空の一覧に戻す", "Reset JSON to an empty list"),
    "reset_question": ("編集中のタスク一覧を空にしますか？保存まではファイルに反映されません。",
                       "Clear the task draft? This affects the file only after saving."),
    "choose": ("選択…", "Browse…"),
    "argv": ("argv（1行1引数。空欄は引数なし。内部の空行は空文字引数）",
             "argv (one argument per line; empty field = no args; internal blank line = empty arg)"),
    "json": ("JSONプレビュー（直接編集可）", "JSON preview (editable)"),
    "check": ("最終内容を確認", "Review final contents"),
    "ack": ("許可フォルダ内のファイルが承認窓なしで読まれることを確認しました", "I understand that allowed files can be read without approval"),
    "save": ("確認した3ファイルを保存", "Save the three reviewed settings"),
    "confirm": ("以下の変更・削除とバックアップ作成を実行しますか？", "Apply these changes/deletions and create backups?"),
    "absent": ("ファイルなし（既存なら削除）", "Absent (delete if currently present)"),
    "restart": ("反映には Broker とクライアントの再起動が必要です。", "Restart both the Broker and client to apply changes."),
    "stopped": ("保存を停止しました。再保存せず、結果とバックアップを確認してください。", "Save stopped. Review results and backups before another save."),
    "changed": ("内容が変わりました。「最終内容を確認」を押してください。", "Draft changed. Review final contents again."),
    "invalid_json": ("先にJSONの形式を直してください。現在の内容は保持しています。", "Correct the JSON structure first. The draft has been preserved."),
}

ROOT_REASONS = {
    "absolute local path required": "引用符・環境変数を使わず、ローカルの絶対パスを指定してください。",
    "drive root refused": "ドライブ全体は許可できません。下のフォルダを選んでください。",
    "protected path name": "秘密情報などの保護対象名を含んでいます。",
    "protected system or user folder": "システムフォルダやユーザーフォルダ全体は許可できません。",
    "contains private configuration or state": "設定・鍵・状態の保存場所を含むフォルダは許可できません。",
    "folder missing, inaccessible, linked or not a directory": "フォルダが見つからないか、アクセスできないか、リンクです。",
    "protected canonical folder": "実際の保存場所が保護対象です。",
    "more than 16 folders": "フルと読むだけを合わせて16件までにしてください。",
    "full and read-only folders overlap": "フルと読むだけのフォルダが同じ、または親子になっています。",
    "no folders configured": "1件以上追加するか、設定保存をオフにしてください。",
    "configuration too large": "設定ファイルは64 KiBまでです。",
    "invalid UTF-8": "UTF-8として読めません。",
}


def task_template(kind, cwd, environ):
    command = "npm.cmd test" if kind == "npm" else "py -3.13 -m pytest"
    return {"name": f"test-{kind}", "description": f"Run {kind} tests",
            "cwd": str(cwd),
            "executable": str(Path(environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
            "argv": ["/d", "/s", "/c", command], "timeout": 1800, "output_limit": 65536}


class Builder:
    def __init__(self, window, root, environ):
        self.window, self.root, self.environ = window, root, dict(environ)
        self.baseline = model.snapshot(environ)
        self.contents = {name: record.data for name, record in self.baseline.items()}
        # Undecodable bytes must never be silently replaced by GUI normalization.
        for data in self.contents.values():
            if data is not None:
                data.decode("utf-8-sig")
        self.locale = "ja"
        self.pending_check = None
        self.reviewed = None
        self.task_index = None
        self.form_dirty = False
        self.filling_form = False
        self.render()

    def t(self, key):
        return COPY[key][self.locale == "en"]

    def render(self):
        self.rendering = True
        if self.pending_check is not None:
            self.window.after_cancel(self.pending_check)
            self.pending_check = None
        for child in self.window.winfo_children():
            child.destroy()
        self.reviewed = None
        self.window.title("Sora Commander Config Builder")
        self.window.geometry("1060x820")
        top = ttk.Frame(self.window, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Sora Commander Config Builder", font=("Segoe UI", 16, "bold")).pack(side="left")
        language = ttk.Combobox(top, values=("日本語", "English"), state="readonly", width=12)
        language.set("日本語" if self.locale == "ja" else "English")
        language.pack(side="right")
        language.bind("<<ComboboxSelected>>", lambda _: self.change_language(language.current()))
        ttk.Label(self.window, text=self.t("warning"), wraplength=1000, foreground="#9b271b", padding=10).pack(fill="x")
        self.tabs = ttk.Notebook(self.window)
        self.tabs.pack(fill="both", expand=True, padx=10)
        pages = []
        for key in ("roots", "language", "tasks", "review"):
            page = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(page, text=self.t(key))
            pages.append(page)
        self.make_roots(pages[0])
        self.make_language(pages[1])
        self.make_tasks(pages[2])
        self.make_review(pages[3])
        self.status = self.text_box(self.window, height=5)
        self.status.configure(state="disabled")
        self.rendering = False
        self.validate()

    def change_language(self, index):
        if self.form_dirty:
            messagebox.showinfo("HOLD", "Apply the task form to JSON before switching UI language.", parent=self.window)
            return
        self.capture_tasks()
        self.locale = "ja" if index == 0 else "en"
        self.render()

    @staticmethod
    def text_box(parent, height=8):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, height=height, wrap="none", undo=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return text

    @staticmethod
    def show(text, value):
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", value)
        text.configure(state="disabled")

    def changed(self):
        self.reviewed = None
        self.ack.set(False)
        self.save_button.configure(state="disabled")
        self.show(self.preview, self.t("changed"))
        if self.pending_check is not None:
            self.window.after_cancel(self.pending_check)
        self.pending_check = self.window.after(250, self.validate)

    def validate(self):
        self.pending_check = None
        self.capture_tasks()
        results = model.validate_candidate(self.contents, self.root, self.environ)
        if self.form_dirty:
            try:
                task = self.form_task()
                model.task_presets.parse_task_presets(json.dumps({"tasks": [task]}).encode("utf-8"))
                detail = "フォームをJSONに反映してください。" if self.locale == "ja" else "Apply the form to JSON before saving."
            except ValueError:
                detail = "編集中のフォームに不正な値があります。" if self.locale == "ja" else "The edited form contains invalid values."
            results.append(("HOLD", "form", detail))
        lines = []
        for level, name, detail in results:
            if self.locale == "ja" and detail.startswith("allowed-roots line "):
                where, _, reason = detail.partition(": ")
                number = where.rsplit(" ", 1)[-1]
                detail = f"{number}行目（0はファイル全体）: {ROOT_REASONS.get(reason, reason)}"
            lines.append(f"[{level}] {name}: {detail}")
        self.show(self.status, "\n".join(lines))
        return not any(level == "HOLD" for level, _, _ in results)

    def make_roots(self, page):
        self.roots_enabled = tk.BooleanVar(value=self.contents[model.NAMES[0]] is not None)
        ttk.Checkbutton(page, text=self.t("roots_enabled"), variable=self.roots_enabled,
                        command=self.update_roots).pack(anchor="w")
        self.root_lines = (self.contents[model.NAMES[0]] or b"").decode("utf-8-sig").splitlines()
        self.roots_list = tk.Listbox(page, height=12, exportselection=False)
        self.roots_list.pack(fill="both", expand=True, pady=8)
        self.refresh_roots()
        buttons = ttk.Frame(page)
        buttons.pack(fill="x")
        for key, action in (("add", self.add_root), ("read", lambda: self.set_root_mode(True)),
                            ("full", lambda: self.set_root_mode(False)), ("remove", self.remove_root)):
            ttk.Button(buttons, text=self.t(key), command=action).pack(side="left", padx=3)
        ttk.Label(page, text=self.t("read_limit"), wraplength=950).pack(anchor="w", pady=12)

    def refresh_roots(self):
        self.roots_list.delete(0, "end")
        self.root_indexes = []
        for index, line in enumerate(self.root_lines):
            if line.strip() and not line.strip().startswith("#"):
                self.root_indexes.append(index)
                self.roots_list.insert("end", f"{index + 1}  {line}")

    def update_roots(self):
        self.contents[model.NAMES[0]] = (("\n".join(self.root_lines) + "\n").encode("utf-8")
                                        if self.roots_enabled.get() else None)
        self.refresh_roots()
        self.changed()

    def add_root(self):
        folder = filedialog.askdirectory(parent=self.window, mustexist=True)
        if folder:
            self.root_lines.append("read: " + folder)
            self.roots_enabled.set(True)
            self.update_roots()

    def set_root_mode(self, reading):
        selected = self.roots_list.curselection()
        if not selected:
            return
        if not reading and not messagebox.askyesno(self.t("full"), self.t("full_warning"), parent=self.window):
            return
        index = self.root_indexes[selected[0]]
        value = self.root_lines[index].strip()
        if value.startswith("read:"):
            value = value[5:].lstrip()
        self.root_lines[index] = ("read: " if reading else "") + value
        self.update_roots()

    def remove_root(self):
        selected = self.roots_list.curselection()
        if selected:
            del self.root_lines[self.root_indexes[selected[0]]]
            self.update_roots()

    def make_language(self, page):
        current = self.contents[model.NAMES[1]]
        try:
            value = model.owner_ui_language._parse(current) if current is not None else ""
        except ValueError:
            value = "invalid"
        ttk.Label(page, text=(current or b"").decode("utf-8-sig"), wraplength=900).pack(anchor="w", pady=10)
        self.owner_language = tk.StringVar(value=value)
        for code, label in (("", self.t("system")), ("en", "English"), ("ja", "日本語")):
            ttk.Radiobutton(page, text=label, value=code, variable=self.owner_language,
                            command=self.set_owner_language).pack(anchor="w", pady=8)

    def set_owner_language(self):
        value = self.owner_language.get()
        self.contents[model.NAMES[1]] = (value + "\n").encode("utf-8") if value else None
        self.changed()

    def make_tasks(self, page):
        self.tasks_enabled = tk.BooleanVar(value=self.contents[model.NAMES[2]] is not None)
        ttk.Checkbutton(page, text=self.t("tasks_enabled"), variable=self.tasks_enabled,
                        command=self.changed).pack(anchor="w")
        layout = ttk.Frame(page)
        layout.pack(fill="x")
        left = ttk.Frame(layout)
        left.pack(side="left", fill="y", padx=(0, 10))
        self.task_list = tk.Listbox(left, height=8, width=22, exportselection=False)
        self.task_list.pack(fill="both", expand=True)
        self.task_list.bind("<<ListboxSelect>>", self.select_task)
        ttk.Button(left, text=self.t("new"), command=lambda: self.fill_task({})).pack(fill="x")
        ttk.Button(left, text=self.t("remove"), command=self.remove_task).pack(fill="x")
        for kind in ("npm", "pytest"):
            ttk.Button(left, text=f"{kind} template", command=lambda k=kind: self.fill_task(task_template(k, self.root, self.environ))).pack(fill="x")
        form = ttk.Frame(layout)
        form.pack(side="left", fill="both", expand=True)
        form.columnconfigure(1, weight=1)
        self.fields = {}
        for row, key in enumerate(("name", "description", "cwd", "executable", "timeout", "output_limit")):
            ttk.Label(form, text=key).grid(row=row, column=0, sticky="w")
            value = tk.StringVar()
            self.fields[key] = value
            value.trace_add("write", self.form_changed)
            ttk.Entry(form, textvariable=value).grid(row=row, column=1, sticky="ew")
            if key in ("cwd", "executable"):
                ttk.Button(form, text=self.t("choose"), command=lambda k=key: self.choose_task_path(k)).grid(row=row, column=2)
        ttk.Label(form, text=self.t("argv")).grid(row=6, column=0, columnspan=3, sticky="w")
        self.argv = tk.Text(form, height=4, wrap="none")
        self.argv.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.argv.bind("<<Modified>>", self.argv_changed)
        ttk.Button(form, text=self.t("apply"), command=self.apply_task).grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(page, text=self.t("json")).pack(anchor="w", pady=(8, 0))
        self.json_text = self.text_box(page, height=8)
        self.json_text.insert("1.0", (self.contents[model.NAMES[2]] or b'{"tasks": []}\n').decode("utf-8-sig"))
        self.json_text.edit_modified(False)
        self.json_text.bind("<<Modified>>", self.json_changed)
        ttk.Button(page, text=self.t("reset"), command=self.reset_tasks).pack(anchor="w")
        self.fill_task({})
        self.refresh_task_list()

    def choose_task_path(self, key):
        chooser = filedialog.askdirectory if key == "cwd" else filedialog.askopenfilename
        value = chooser(parent=self.window)
        if value:
            self.fields[key].set(value)

    def capture_tasks(self):
        # Preserve untouched BOMs/formatting by only capturing edited JSON.
        if not self.tasks_enabled.get():
            self.contents[model.NAMES[2]] = None
        elif self.contents[model.NAMES[2]] is None or self.json_text.edit_modified():
            self.contents[model.NAMES[2]] = self.json_text.get("1.0", "end-1c").encode("utf-8")

    def json_changed(self, _=None):
        if self.json_text.edit_modified():
            self.contents[model.NAMES[2]] = (self.json_text.get("1.0", "end-1c").encode("utf-8")
                                           if self.tasks_enabled.get() else None)
            self.json_text.edit_modified(False)
            self.task_index = None
            self.refresh_task_list()
            self.changed()

    def task_document(self):
        try:
            doc = json.loads(self.json_text.get("1.0", "end-1c"), object_pairs_hook=model.task_presets._no_duplicates)
            if type(doc) is not dict or set(doc) != {"tasks"} or type(doc["tasks"]) is not list:
                raise ValueError()
            return doc
        except ValueError:
            return None

    def refresh_task_list(self):
        self.task_list.delete(0, "end")
        doc = self.task_document()
        if doc is not None:
            for index, task in enumerate(doc["tasks"]):
                name = task.get("name", "?") if type(task) is dict else "?"
                self.task_list.insert("end", f"{index + 1}: {name}")

    def fill_task(self, task, index=None):
        self.filling_form = True
        self.task_index = index
        for key, variable in self.fields.items():
            variable.set(str(task.get(key, {"timeout": 1800, "output_limit": 65536}.get(key, ""))))
        self.argv.delete("1.0", "end")
        arguments = task.get("argv", [])
        self.argv.insert("1.0", "\n".join(arguments))
        self.argv.edit_modified(False)
        self.filling_form = False
        self.form_dirty = bool(task) and index is None
        if not self.rendering:
            self.changed()

    def form_changed(self, *_):
        if not self.filling_form and not self.rendering:
            self.form_dirty = True
            self.changed()

    def argv_changed(self, _=None):
        if self.argv.edit_modified():
            self.argv.edit_modified(False)
            self.form_changed()

    def select_task(self, _=None):
        selected = self.task_list.curselection()
        doc = self.task_document()
        if selected and doc is not None:
            task = doc["tasks"][selected[0]]
            if type(task) is dict:
                arguments = task.get("argv", [])
                if type(arguments) is not list or any(type(a) is not str or "\n" in a or "\r" in a for a in arguments) or arguments == [""]:
                    messagebox.showinfo("JSON", "Edit this argv directly in JSON to preserve empty or multiline arguments.", parent=self.window)
                    return
                self.fill_task(task, selected[0])

    def put_tasks(self, doc):
        self.form_dirty = False
        self.tasks_enabled.set(True)
        self.json_text.delete("1.0", "end")
        self.json_text.insert("1.0", json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        self.json_changed()

    def form_task(self):
        task = {key: variable.get() for key, variable in self.fields.items()}
        text = self.argv.get("1.0", "end-1c")
        task["argv"] = text.split("\n") if text else []
        task["timeout"] = float(task["timeout"])
        task["output_limit"] = int(task["output_limit"])
        return task

    def apply_task(self):
        doc = self.task_document()
        if doc is None:
            messagebox.showerror("JSON", self.t("invalid_json"), parent=self.window)
            return
        try:
            task = self.form_task()
        except ValueError:
            messagebox.showerror("HOLD", "timeout / output_limit: numeric value required", parent=self.window)
            return
        if self.task_index is None:
            doc["tasks"].append(task)
        else:
            doc["tasks"][self.task_index] = task
        self.put_tasks(doc)

    def remove_task(self):
        doc = self.task_document()
        selected = self.task_list.curselection()
        if doc is not None and selected:
            del doc["tasks"][selected[0]]
            self.put_tasks(doc)

    def reset_tasks(self):
        if messagebox.askyesno("JSON", self.t("reset_question"), parent=self.window):
            self.put_tasks({"tasks": []})

    def make_review(self, page):
        ttk.Button(page, text=self.t("check"), command=self.review).pack(anchor="w")
        self.preview = self.text_box(page, height=18)
        self.preview.configure(state="disabled")
        self.ack = tk.BooleanVar(value=False)
        ttk.Checkbutton(page, text=self.t("ack"), variable=self.ack, command=self.enable_save).pack(anchor="w", pady=8)
        self.save_button = ttk.Button(page, text=self.t("save"), command=self.save, state="disabled")
        self.save_button.pack(anchor="w")

    def review(self):
        self.ack.set(False)
        self.save_button.configure(state="disabled")
        valid = self.validate()
        self.reviewed = dict(self.contents) if valid else None
        parts = []
        for name in model.NAMES:
            data = self.contents[name]
            added, removed = model.difference(self.baseline[name].data, data)
            parts.append(f"--- {name} (+{added} / -{removed}) ---\n" +
                         (self.t("absent") if data is None else data.decode("utf-8-sig")))
        self.show(self.preview, "\n\n".join(parts))

    def enable_save(self):
        enabled = self.ack.get() and self.reviewed is not None and self.reviewed == self.contents
        self.save_button.configure(state="normal" if enabled else "disabled")

    def save(self):
        self.capture_tasks()
        if not self.ack.get() or self.reviewed is None or self.reviewed != self.contents:
            return
        changed = [name for name in model.NAMES if self.contents[name] != self.baseline[name].data]
        actions = [name + (" [DELETE]" if self.contents[name] is None else " [WRITE]") for name in changed]
        if not changed or not messagebox.askyesno(self.t("save"), self.t("confirm") + "\n\n" + "\n".join(actions), parent=self.window):
            return
        self.save_button.configure(state="disabled")
        try:
            backups, results = model.save(dict(self.reviewed), self.baseline, self.root,
                                          self.environ, acknowledged=True)
            self.baseline = model.snapshot(self.environ)
            self.ack.set(False)
            self.reviewed = None
            detail = "\n".join(f"[{level}] {name}: {reason}" for level, name, reason in results)
            self.show(self.preview, detail + "\n\n" + "\n".join(backups.values()) + "\n\n" + self.t("restart"))
            messagebox.showinfo(self.t("save"), self.t("restart"), parent=self.window)
        except (OSError, ValueError, RuntimeError) as error:
            self.ack.set(False)
            self.reviewed = None
            detail = self.t("stopped")
            if isinstance(error, model.SaveError):
                detail += "\n" + error.failure
                detail += "\nCompleted: " + ", ".join(error.completed)
                detail += "\nBackups:\n" + "\n".join(error.backups.values())
            elif isinstance(error, model.ConfigError):
                detail += "\n" + str(error)
            else:
                detail += f"\n{type(error).__name__} / WinError {getattr(error, 'winerror', None)}"
            self.show(self.preview, detail)
            messagebox.showerror("HOLD", detail, parent=self.window)


def launch(root):
    window = tk.Tk()
    try:
        Builder(window, root, os.environ)
    except (OSError, ValueError, RuntimeError) as error:
        detail = str(error) if isinstance(error, model.ConfigError) else type(error).__name__
        messagebox.showerror("HOLD", "設定を安全に読み込めませんでした。\n" + detail, parent=window)
        window.destroy()
        return 1
    window.mainloop()
    return 0
