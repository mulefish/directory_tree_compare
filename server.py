"""A small, dependency-free Windows folder comparison app."""

from __future__ import annotations

import argparse
from collections import Counter
import difflib
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from last_paths import default_storage_file, load_last_paths, save_last_paths
from tree_compare import Entry, compare_trees


MAX_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_DIFF_LINES = 4000


def read_text(path: Path | None) -> tuple[list[str], str]:
    if path is None:
        return [], ""
    try:
        if path.stat().st_size > MAX_PREVIEW_BYTES:
            return [], f"Preview unavailable: file is larger than {MAX_PREVIEW_BYTES // 1024 // 1024} MB."
        data = path.read_bytes()
    except OSError as exc:
        return [], f"Cannot read file: {exc}"
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return [], "Binary file: text preview unavailable."
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            decoded = data.decode("utf-16")
        else:
            try:
                decoded = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                decoded = data.decode("cp1252")
    except UnicodeError as exc:
        return [], f"Cannot decode text: {exc}"
    lines = decoded.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        return [], f"Preview unavailable: file has more than {MAX_DIFF_LINES:,} lines."
    return lines, ""


def aligned_lines(left: list[str], right: list[str]):
    """Yield aligned (left text, right text, tag) rows for a two-pane diff."""
    matcher = difflib.SequenceMatcher(None, left, right)
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        for offset in range(max(a2 - a1, b2 - b1)):
            a = left[a1 + offset] if a1 + offset < a2 else ""
            b = right[b1 + offset] if b1 + offset < b2 else ""
            yield a, b, tag


class CompareApp(tk.Tk):
    def __init__(self, left: str = "", right: str = "", storage_path: Path | None = None) -> None:
        super().__init__()
        self.title("Tree Compare")
        self.geometry("1200x780")
        self.minsize(800, 500)
        self.left_var = tk.StringVar(value=left)
        self.right_var = tk.StringVar(value=right)
        self.storage_path = storage_path or default_storage_file()
        self.differences_only = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose two folders, then click Compare.")
        self.entries: list[Entry] = []
        self.by_path: dict[str, Entry] = {}
        self.results: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build_ui()
        self.after(100, self._poll_results)
        if left and right:
            self.after(150, self._start_scan)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        for row, (caption, variable) in enumerate((("Left folder", self.left_var),
                                                   ("Right folder", self.right_var))):
            ttk.Label(top, text=caption).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(top, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(top, text="Browse…", command=lambda v=variable: self._browse(v)).grid(
                row=row, column=2, padx=(8, 0), pady=3)
        self.compare_button = ttk.Button(top, text="Compare", command=self._start_scan)
        self.compare_button.grid(row=0, column=3, rowspan=2, padx=(12, 0), sticky="ns")
        self.use_last_button = ttk.Button(top, text="Use last", command=self._use_last)
        self.use_last_button.grid(row=0, column=4, rowspan=2, padx=(8, 0), sticky="ns")

        controls = ttk.Frame(self, padding=(10, 0, 10, 8))
        controls.pack(fill="x")
        ttk.Checkbutton(controls, text="Show differences only", variable=self.differences_only,
                        command=self._fill_tree).pack(side="left")
        ttk.Label(controls, textvariable=self.status_var).pack(side="right")

        splitter = ttk.Panedwindow(self, orient="vertical")
        splitter.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        upper = ttk.Frame(splitter)
        splitter.add(upper, weight=3)
        upper.rowconfigure(0, weight=1)
        upper.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(upper, columns=("status", "left", "right"), show="tree headings")
        self.tree.heading("#0", text="Relative path")
        self.tree.heading("status", text="Status")
        self.tree.heading("left", text="Left")
        self.tree.heading("right", text="Right")
        self.tree.column("#0", width=440, minwidth=220)
        self.tree.column("status", width=130, stretch=False)
        self.tree.column("left", width=100, stretch=False, anchor="center")
        self.tree.column("right", width=100, stretch=False, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(upper, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("Different", foreground="#9b3a00")
        self.tree.tag_configure("Left only", foreground="#a52234")
        self.tree.tag_configure("Right only", foreground="#14599a")
        self.tree.tag_configure("Error", foreground="#a52234")
        self.tree.bind("<<TreeviewSelect>>", self._show_selection)

        lower = ttk.Frame(splitter)
        splitter.add(lower, weight=2)
        lower.columnconfigure(0, weight=1)
        lower.columnconfigure(1, weight=1)
        lower.rowconfigure(1, weight=1)
        self.left_label = ttk.Label(lower, text="Left preview (red = removed or changed)")
        self.right_label = ttk.Label(lower, text="Right preview (green = added or changed)")
        self.left_label.grid(row=0, column=0, sticky="w")
        self.right_label.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.left_text = tk.Text(lower, wrap="none", font=("Consolas", 10), state="disabled")
        self.right_text = tk.Text(lower, wrap="none", font=("Consolas", 10), state="disabled")
        self.left_text.grid(row=1, column=0, sticky="nsew")
        self.right_text.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        self.left_text.tag_configure("replace", background="#f9b7c0", foreground="#4a1019")
        self.left_text.tag_configure("delete", background="#f9b7c0", foreground="#4a1019")
        self.right_text.tag_configure("replace", background="#a8e5b8", foreground="#123d20")
        self.right_text.tag_configure("insert", background="#a8e5b8", foreground="#123d20")
        for widget in (self.left_text, self.right_text):
            widget.tag_configure("missing", background="#e9e9e9")
        preview_scroll = ttk.Scrollbar(lower, orient="vertical", command=self._scroll_previews)
        preview_scroll.grid(row=1, column=2, sticky="ns")
        self.left_text.configure(yscrollcommand=preview_scroll.set)
        self.right_text.configure(yscrollcommand=preview_scroll.set)

    def _browse(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(initialdir=variable.get() or None)
        if selected:
            variable.set(selected)

    def _use_last(self) -> None:
        paths = load_last_paths(self.storage_path)
        if paths is None:
            messagebox.showinfo("Use last", "No saved folder pair is available yet.")
            return
        self.left_var.set(paths[0])
        self.right_var.set(paths[1])

    def _start_scan(self) -> None:
        left, right = self.left_var.get().strip(), self.right_var.get().strip()
        if not left or not right:
            messagebox.showerror("Folders needed", "Choose both folders.")
            return
        try:
            save_last_paths(left, right, self.storage_path)
        except OSError as exc:
            messagebox.showwarning("Could not save folders", f"Comparison will continue, but the paths could not be saved: {exc}")
        self.compare_button.configure(state="disabled")
        self.status_var.set("Comparing…")
        self.tree.delete(*self.tree.get_children())
        self._clear_preview()

        def work() -> None:
            try:
                self.results.put(("success", compare_trees(left, right)))
            except Exception as exc:
                self.results.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            kind, value = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.compare_button.configure(state="normal")
            if kind == "success":
                self.entries = value
                self.by_path = {item.relative_path: item for item in self.entries}
                self._fill_tree()
                counts = Counter(item.status for item in self.entries if item.kind != "folder")
                self.status_var.set(f"{len(self.entries)} items · {counts['Different']} changed · "
                                    f"{counts['Left only']} left only · {counts['Right only']} right only")
            else:
                self.status_var.set("Comparison failed.")
                messagebox.showerror("Comparison failed", str(value))
        self.after(100, self._poll_results)

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        visible = [item for item in self.entries if not self.differences_only.get() or item.status != "Same"]
        for item in visible:
            parent = Path(item.relative_path).parent.as_posix()
            parent_id = parent if self.tree.exists(parent) else ""
            name = Path(item.relative_path).name
            if item.kind == "folder":
                name += "/"
            self.tree.insert(parent_id, "end", iid=item.relative_path, text=name,
                             values=(item.status, "✓" if item.left else "", "✓" if item.right else ""),
                             tags=(item.status,))
        if self.differences_only.get():
            for item in visible:
                if item.kind == "folder" and self.tree.exists(item.relative_path):
                    self.tree.item(item.relative_path, open=True)

    def _clear_preview(self) -> None:
        for widget in (self.left_text, self.right_text):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        self.left_label.configure(text="Left preview (red = removed or changed)")
        self.right_label.configure(text="Right preview (green = added or changed)")

    def _show_selection(self, _event=None) -> None:
        selected = self.tree.selection()
        self._clear_preview()
        if not selected:
            return
        item = self.by_path[selected[0]]
        self.left_label.configure(text=str(item.left or "Left: absent"))
        self.right_label.configure(text=str(item.right or "Right: absent"))
        if item.kind != "file":
            self._write_preview(item.detail or f"{item.kind.capitalize()}: {item.status}", "")
            return
        left, left_error = read_text(item.left)
        right, right_error = read_text(item.right)
        if left_error or right_error:
            self._write_preview(left_error or "Preview unavailable for the other file.",
                                right_error or "Preview unavailable for the other file.")
            return
        rows = list(aligned_lines(left, right))
        if item.status == "Different" and not any(row[2] != "equal" for row in rows):
            note = "Files differ in bytes, but decoded text matches. Check encoding or byte order marks."
            rows.insert(0, (note, note, "replace"))
        first_change = next((number for number, row in enumerate(rows, start=1)
                             if row[2] != "equal"), None)
        for widget, column in ((self.left_text, 0), (self.right_text, 1)):
            widget.configure(state="normal")
            widget.insert("end", "".join(row[column] + "\n" for row in rows))
            for line_number, row in enumerate(rows, start=1):
                tag = row[2]
                if tag != "equal":
                    shown_tag = "missing" if (tag == "delete" and column == 1 or
                                              tag == "insert" and column == 0) else tag
                    widget.tag_add(shown_tag, f"{line_number}.0", f"{line_number + 1}.0")
            widget.configure(state="disabled")
            if first_change is not None:
                widget.see(f"{first_change}.0")

    def _write_preview(self, left: str, right: str) -> None:
        for widget, content in ((self.left_text, left), (self.right_text, right)):
            widget.configure(state="normal")
            widget.insert("end", content)
            widget.configure(state="disabled")

    def _scroll_previews(self, *args) -> None:
        self.left_text.yview(*args)
        self.right_text.yview(*args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two directory trees.")
    parser.add_argument("left", nargs="?", help="Left folder")
    parser.add_argument("right", nargs="?", help="Right folder")
    parser.add_argument("--json", action="store_true", help="Print machine-readable results instead of opening the app")
    args = parser.parse_args()
    if args.json:
        if not args.left or not args.right:
            parser.error("--json requires both folders")
        entries = compare_trees(args.left, args.right)
        print(json.dumps([{"path": e.relative_path, "kind": e.kind, "status": e.status,
                           "detail": e.detail} for e in entries], indent=2))
    else:
        CompareApp(args.left or "", args.right or "").mainloop()


if __name__ == "__main__":
    main()
