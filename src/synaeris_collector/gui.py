"""Small reusable Windows desktop recorder; all capture work stays off the UI thread."""
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
from .config import Config
from .collector import Collector
from .dataset import inspect_episode, transitions, decision_windows, build_dataset
from .bgi import make_batch, deploy_batch
from .capture import GameWindow
from .auto_index import organize
from .decision_units import write_episode_sidecar
from .task_review import append_task_review, OUTCOMES
from .human import (SCENARIOS, recommended_config, profile_descriptor, save_preferences,
                    ensure_port_available, wait_for_human_game)
from collections import deque




class App:
    def __init__(self, config=None, startup_note=""):
        self.root = tk.Tk()
        self.root.title("Synaeris Collector · 人工采集")
        self.root.geometry("900x640")
        self.root.minsize(760, 560)
        self.root.configure(bg="#111722")
        self.root.protocol("WM_DELETE_WINDOW", self.exit)
        self.root.bind("<F10>", lambda _: self.stop())
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#111722")
        style.configure("TLabel", background="#111722", foreground="#e1e7f0", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=10, font=("Microsoft YaHei UI", 11), background="#263244", foreground="#e1e7f0")
        style.map("TButton", background=[("active", "#334964")])
        style.configure("Primary.TButton", background="#7ae4cc", foreground="#07231a")
        style.map("Primary.TButton", background=[("active", "#a2f0de"), ("disabled", "#263244")])
        style.configure("TCheckbutton", background="#111722", foreground="#e1e7f0")
        style.configure("TNotebook", background="#111722")
        style.configure("TNotebook.Tab", padding=(16, 8), background="#263244", foreground="#e1e7f0")
        self.config = config or recommended_config(Config(scenario=SCENARIOS[0]))
        self.messages = queue.Queue()
        self.collector = self.thread = self.latest = self.photo = None
        self.recording = self.busy = False
        self.settings_window = self.log_window = self.log = None
        self.log_lines = deque(maxlen=500)
        self.fields = {key: tk.StringVar(value=str(getattr(self.config, key)))
                       for key in ["output", "actor", "region", "quest", "puzzle", "poi", "capture", "codec"]}
        self.numeric_fields = {key: tk.StringVar(value=str(getattr(self.config, key)))
                               for key in ["fps", "ocr_threads", "ocr_max_hz", "storage_budget_gb", "reserve_free_gb"]}
        self.scenario = tk.StringVar(value=self.config.scenario if self.config.scenario != "unspecified" else SCENARIOS[0])
        self.standard = tk.BooleanVar(value=profile_descriptor(self.config)["standard"])
        self.novel = tk.BooleanVar(value=self.config.novel)
        self.ood = tk.BooleanVar(value=self.config.ood)
        self.ocr_enabled = tk.BooleanVar(value=self.config.ocr)
        self.background = tk.BooleanVar(value=self.config.capture_background)
        self.note = tk.StringVar()
        self.status = tk.StringVar(value="准备就绪 · 打开原神后点开始")
        self.progress = tk.StringVar(value="保存位置已准备好，停止后自动保存并校验")
        self.last_episode = tk.StringVar(value="无需游玩时打标签 · 停止后自动整理 · F10 停止；F6–F9标记可选")
        self.profile_label = tk.StringVar()
        self.session_widgets, self.parameter_widgets = [], []
        menu = tk.Menu(self.root)
        settings = tk.Menu(menu, tearoff=False)
        settings.add_command(label="高级设置…", command=self.show_settings)
        settings.add_command(label="恢复统一默认参数", command=self.restore_defaults)
        settings.add_command(label="检查游戏与权限", command=self.preflight)
        settings.add_command(label="查看日志…", command=self.show_log)
        menu.add_cascade(label="设置", menu=settings)
        data = tk.Menu(menu, tearoff=False)
        for label, command in [("打开数据目录", self.open_output), ("打开最近录像", self.open_latest),
                               ("自动整理已有录像…", self.organize_existing), ("打开最近片段索引", self.open_index),
                               ("补录任务与结果…", self.record_task_review),
                               ("校验已有录像…", self.review), ("生成 BGI 批次…", self.batch)]:
            data.add_command(label=label, command=command)
        menu.add_cascade(label="数据", menu=data)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="使用说明", command=lambda: messagebox.showinfo("人工采集", "直接双击 SynaerisCollector.exe，完成正常 Windows 确认后点开始。\n无需手动标签；停止后自动整理候选片段并校验，F10停止。\nF6–F9思考/错误/发现/子目标标记可选。自动标签供之后复核，不代表成功或人的思考。\n首次探索、解谜、失败与接管尽量录完整过程。"))
        menu.add_cascade(label="帮助", menu=help_menu)
        self.root.configure(menu=menu)
        body = ttk.Frame(self.root, padding=26)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Synaeris Collector", font=("Microsoft YaHei UI", 24, "bold")).pack(anchor="w")
        ttk.Label(body, text="打开原神 → 开始采集 → 正常游玩", foreground="#8fabc7").pack(anchor="w", pady=(6, 18))
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, text="本次场景").pack(side="left", padx=(0, 12))
        scene = ttk.Combobox(row, textvariable=self.scenario, values=SCENARIOS, state="readonly", width=18)
        scene.pack(side="left")
        scene.bind("<<ComboboxSelected>>", lambda _: self.novel.set(self.scenario.get() != "熟练操作"))
        self.session_widgets.append(scene)
        ttk.Label(row, textvariable=self.profile_label, foreground="#7ae4cc").pack(side="right")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(18, 12))
        self.start_button = ttk.Button(actions, text="开始采集", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left", ipadx=26)
        self.stop_button = ttk.Button(actions, text="停止并保存", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=12)
        ttk.Button(actions, text="收起", command=self.root.iconify).pack(side="right")
        ttk.Label(body, textvariable=self.status, foreground="#7ae4cc", wraplength=820).pack(anchor="w", pady=6)
        ttk.Label(body, textvariable=self.progress, foreground="#9fb0c5", wraplength=820).pack(anchor="w", pady=4)
        self.preview = tk.Label(body, text="游戏画面预览\n采集器可收起，游戏保持前台", bg="#070c14", fg="#65819a", height=12)
        self.preview.pack(fill="both", expand=True, pady=14)
        ttk.Label(body, textvariable=self.last_episode, foreground="#9fb0c5", wraplength=820).pack(anchor="w")
        self.update_profile_label()
        if startup_note:
            self.write_log(startup_note)
        self.root.after(200, self.tick)
        if getattr(sys, 'frozen', False):
            self.root.after(1500, self.check_updates_background)

    def check_updates_background(self):
        def run():
            from .updater import check_and_stage
            check_and_stage(announce=lambda value: self.messages.put(('update', value)))
        threading.Thread(target=run, name='collector-update', daemon=True).start()

    def current_config(self):
        numeric = {key: (int(var.get()) if key in {"fps", "ocr_threads"} else float(var.get()))
                   for key, var in self.numeric_fields.items()}
        fields = {key: var.get() for key, var in self.fields.items()}
        custom = replace(self.config, **fields, **numeric, scenario=self.scenario.get(),
                         novel=self.novel.get(), ood=self.ood.get(), ocr=self.ocr_enabled.get(),
                         capture_background=self.background.get())
        if self.standard.get():
            return recommended_config(custom)
        if self.background.get():
            custom = replace(custom, capture="wgc", purpose="calibration")
        else:
            custom = replace(custom, purpose="gameplay")
        return custom.validate()

    def update_profile_label(self):
        self.profile_label.set("统一配置 · 30 FPS · 本地 OCR" if self.standard.get() else "自定义参数 · 单独标记")

    def restore_defaults(self):
        if self.busy:
            return
        self.config = recommended_config(self.config)
        self.standard.set(True)
        for key, var in {**self.fields, **self.numeric_fields}.items():
            if key not in {"output", "region", "quest", "puzzle", "poi"}:
                var.set(str(getattr(self.config, key)))
        self.ocr_enabled.set(True)
        self.background.set(False)
        self.sync_settings()

    def sync_settings(self):
        self.update_profile_label()
        for widget in self.session_widgets:
            if widget.winfo_exists():
                widget.state(["disabled"] if self.busy else ["!disabled"])
        for widget in self.parameter_widgets:
            if widget.winfo_exists():
                widget.state(["disabled"] if self.busy or self.standard.get() else ["!disabled"])

    def show_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        window = self.settings_window = tk.Toplevel(self.root)
        window.title("高级设置")
        window.geometry("860x680")
        window.configure(bg="#111722")
        tabs = ttk.Notebook(window)
        tabs.pack(fill="both", expand=True, padx=18, pady=18)
        metadata, parameters = [ttk.Frame(tabs, padding=18) for _ in range(2)]
        tabs.add(metadata, text="任务与保存")
        tabs.add(parameters, text="录制与 OCR")
        self.session_widgets = [widget for widget in self.session_widgets if widget.winfo_exists()]
        self.parameter_widgets = []
        ttk.Label(metadata, text="区域和任务可选；不确定时保留 unknown。", foreground="#9fb0c5").pack(anchor="w", pady=(0, 16))
        for key, label in [("output", "保存目录"), ("region", "区域"), ("quest", "任务"), ("puzzle", "机关 / Puzzle"), ("poi", "地点 / POI")]:
            row = ttk.Frame(metadata)
            row.pack(fill="x", pady=7)
            ttk.Label(row, text=label, width=15).pack(side="left")
            widget = ttk.Entry(row, textvariable=self.fields[key])
            widget.pack(side="left", fill="x", expand=True)
            self.session_widgets.append(widget)
        for text, variable in [("首次探索 / 高价值，保留完整视频", self.novel), ("OOD 测试数据", self.ood)]:
            widget = ttk.Checkbutton(metadata, text=text, variable=variable)
            widget.pack(anchor="w", pady=8)
            self.session_widgets.append(widget)
        for label, command in [("选择保存目录", self.choose_output), ("导入配置…", self.load), ("导出配置…", self.save)]:
            widget = ttk.Button(metadata, text=label, command=command)
            widget.pack(anchor="w", pady=5)
            self.session_widgets.append(widget)
        def toggle():
            if self.standard.get():
                self.restore_defaults()
            else:
                self.sync_settings()
        widget = ttk.Checkbutton(parameters, text="使用统一默认参数（推荐）", variable=self.standard, command=toggle)
        widget.pack(anchor="w", pady=(0, 10))
        self.session_widgets.append(widget)
        ttk.Label(parameters, text="修改参数会标记为自定义数据；默认参数适合统一分发。", foreground="#9fb0c5").pack(anchor="w", pady=(0, 10))
        for key, label, values in [("actor", "操作者", ["HUMAN", "BGI", "AGENT"]), ("capture", "截图方式", ["auto", "dxcam", "mss", "wgc"]),
                                   ("codec", "编码", ["auto", "av1_nvenc", "hevc_nvenc", "libx264"]),
                                   ("fps", "录像 FPS", [15, 30, 60]), ("ocr_threads", "OCR 线程", [1, 2, 4]),
                                   ("ocr_max_hz", "OCR 上限 Hz", [2, 4, 8]), ("storage_budget_gb", "数据预算 GB", [100, 200, 500]),
                                   ("reserve_free_gb", "磁盘保留 GB", [25, 50, 100])]:
            row = ttk.Frame(parameters)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label, width=18).pack(side="left")
            widget = ttk.Combobox(row, textvariable=({**self.fields, **self.numeric_fields})[key], values=values, width=22,
                                  state="readonly" if key in self.fields else "normal")
            widget.pack(side="left")
            self.parameter_widgets.append(widget)
        for text, variable in [("本地 CPU OCR", self.ocr_enabled), ("实验后台观察（仅视觉校准）", self.background)]:
            widget = ttk.Checkbutton(parameters, text=text, variable=variable)
            widget.pack(anchor="w", pady=6)
            self.parameter_widgets.append(widget)
        widget = ttk.Button(parameters, text="OCR 裁剪区域…", command=self.edit_rois)
        widget.pack(anchor="w", pady=6)
        self.parameter_widgets.append(widget)
        self.sync_settings()

    def show_log(self):
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        self.log_window = tk.Toplevel(self.root)
        self.log_window.title("采集日志")
        self.log = tk.Text(self.log_window, width=100, height=26, bg="#182130", fg="#cdd9e7", wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.insert("end", "\n".join(self.log_lines))
        self.log.configure(state="disabled")

    def preflight(self):
        try:
            window = GameWindow(self.config.window_title)
            p = window.permissions()
            self.write_log(f"游戏 {window.title} · 窗口 {window.initial_rect} · 输入权限 {'通过' if p['input_permission_qualified'] else '不足'}")
            if not p["input_permission_qualified"]:
                self.status.set("原神以管理员权限运行；请双击 SynaerisCollector.exe 并完成 Windows 确认")
            else:
                self.status.set("游戏与输入权限检查通过；进入目标场景后开始采集")
        except Exception as exc:
            self.status.set(str(exc))
            self.write_log(str(exc))

    def choose_output(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title="选择数据保存目录")
        if path:
            self.fields["output"].set(path)

    def load(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="导入采集配置", filetypes=[("采集配置", "*.json")])
        if path:
            try:
                self.config = Config.load(path)
                for key, var in {**self.fields, **self.numeric_fields}.items():
                    var.set(str(getattr(self.config, key)))
                for var, value in [(self.novel, self.config.novel), (self.ocr_enabled, self.config.ocr),
                                   (self.ood, self.config.ood), (self.background, self.config.capture_background),
                                   (self.scenario, self.config.scenario)]:
                    var.set(value)
                self.standard.set(profile_descriptor(self.config)["standard"])
                self.sync_settings()
                self.write_log("配置已导入：" + path)
            except Exception as exc:
                messagebox.showerror("配置导入失败", str(exc))

    def open_output(self):
        path = Path(self.fields["output"].get()).resolve()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def open_latest(self):
        if self.latest:
            os.startfile(self.latest)
        else:
            self.status.set("还没有本次保存的录像；数据菜单可打开全部数据目录")

    def write_log(self, message):
        line = f"{datetime.now():%H:%M:%S}  {message}"
        self.log_lines.append(line)
        if self.log and self.log.winfo_exists():
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(self.log_lines))
            self.log.see("end")
            self.log.configure(state="disabled")

    def start(self):
        if self.busy:
            return
        try:
            config = self.current_config()
            ensure_port_available(config.bridge_port)
            save_preferences(config)
        except Exception as exc:
            self.status.set(str(exc))
            return
        self.collector = Collector(config, lambda msg: self.messages.put(("log", msg)))
        self.recording = self.busy = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.sync_settings()
        self.status.set("正在准备采集；恢复原神后自动开始")
        self.root.iconify()
        def run():
            try:
                self.collector.storage_check()
                if not wait_for_human_game(config, self.collector.stop_event, lambda msg: self.messages.put(("log", msg))):
                    self.messages.put(("cancelled", "已取消等待，未生成空录像"))
                    return
                path = self.collector.run()
                self.messages.put(("saved", str(path)))
                index = organize(path)
                self.messages.put(("indexed", (str(path), len(index['segments']), len(index['labels']))))
                try:
                    audit = write_episode_sidecar(path)
                    self.messages.put(("compiled", audit['total']))
                except Exception as exc:
                    self.messages.put(("log", "语义候选整理失败；母数据已保存："+str(exc)))
                quality = inspect_episode(path)
                decision_windows(path)
                self.messages.put(("checked", (str(path), quality)))
            except Exception as exc:
                self.messages.put(("error", str(exc)))
        self.thread = threading.Thread(target=run, name="human-collection", daemon=False)
        self.thread.start()

    def stop(self):
        if self.recording and self.collector:
            self.collector.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("正在保存并校验，请稍候…")

    def mark(self, kind):
        if self.recording:
            self.collector.mark(kind, self.note.get())
            self.note.set("")
            self.write_log("标记："+kind+" · 前15秒 / 后20秒")

    def save(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="config.local.json")
        if path:
            try:
                self.config = self.current_config()
                self.config.save(path)
                self.write_log("配置保存："+path)
            except Exception as exc:
                messagebox.showerror("配置保存失败", str(exc))

    def edit_rois(self):
        if self.busy:
            messagebox.showinfo("裁剪区域", "停止采集后可以修改 OCR 裁剪区域")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("归一化 OCR 区域 [左, 上, 右, 下]")
        text = tk.Text(dialog, width=65, height=18)
        text.pack(padx=16, pady=16)
        text.insert("1.0", json.dumps(self.config.rois, ensure_ascii=False, indent=2))
        def apply():
            try:
                self.config = replace(self.config, rois=json.loads(text.get("1.0", "end"))).validate()
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("区域无效", str(exc))
        ttk.Button(dialog, text="应用", command=apply).pack(pady=(0, 16))

    def batch(self):
        files = filedialog.askopenfilenames(title="选择现有 BGI 路线", initialdir=str(Path(self.config.bgi_dir)/"User"/"AutoPathing"), filetypes=[("BGI 路线", "*.json")])
        if not files:
            return
        parent = filedialog.askdirectory(title="选择保存批次的父目录")
        if not parent:
            return
        try:
            destination = Path(parent) / ("SynaerisBatch_"+datetime.now().strftime("%Y%m%d_%H%M%S"))
            path = make_batch(files, destination, self.config.bridge_port)
            self.write_log("BGI 批次已生成："+str(path))
            messagebox.showinfo("BGI 批次", "批次已生成。把整个目录放入 BGI/User/JsScript，在调度组启用此脚本的 HTTP 权限，再开始采集并运行批次。")
        except Exception as exc:
            messagebox.showerror("批次生成失败", str(exc))

    def review(self):
        if self.busy:
            messagebox.showinfo("数据校验", "停止采集并保存 Episode 后再校验")
            return
        path = filedialog.askdirectory(title="选择一个 Episode 目录", initialdir=self.config.output)
        if not path:
            return
        def run():
            try:
                report = inspect_episode(path)
                count = len(transitions(path))
                decision_windows(path)
                self.messages.put(("log", f"校验完成 · {report['decoded_frames']}帧 · {count}条工具转换 · 问题 {report['issues']}"))
            except Exception as exc:
                self.messages.put(("log", "数据校验失败："+str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def record_task_review(self):
        if self.busy or self.recording:
            messagebox.showinfo("任务补录", "请先停止采集并保存录像")
            return
        path=self.latest if self.latest and (Path(self.latest)/'manifest.json').exists() else None
        if path is None:
            path=filedialog.askdirectory(title="选择已完成的人工 Episode",initialdir=self.config.output)
        if not path:return
        try:
            manifest=json.loads((Path(path)/'manifest.json').read_text(encoding='utf-8-sig'))
            if manifest['status']!='complete' or manifest['config']['actor']!='HUMAN':
                raise ValueError('请选择完整人工采集 Episode')
        except Exception as error:
            messagebox.showerror("任务补录",str(error));return
        dialog=tk.Toplevel(self.root);dialog.title('任务与结果 · 人工声明待复核')
        dialog.geometry('570x520');dialog.transient(self.root)
        fields={'start_seconds':tk.StringVar(value='0'),
            'end_seconds':tk.StringVar(value=f"{manifest['duration_ns']/1e9:.9f}"),
            'scenario':tk.StringVar(value=manifest['config'].get('scenario',SCENARIOS[0])),
            **{name:tk.StringVar(value=manifest['config'].get(name,'unknown'))
                for name in ('region','quest','puzzle','poi')},
            'outcome':tk.StringVar(value='unknown')}
        names={'start_seconds':'开始秒数','end_seconds':'结束秒数','scenario':'场景',
            'region':'区域','quest':'任务','puzzle':'机关','poi':'地点',
            'outcome':'结果声明'}
        form=ttk.Frame(dialog,padding=16);form.pack(fill='both',expand=True)
        for row,(name,label) in enumerate(names.items()):
            ttk.Label(form,text=label).grid(row=row,column=0,sticky='w',pady=4)
            if name in ('scenario','outcome'):
                values=SCENARIOS if name=='scenario' else OUTCOMES
                widget=ttk.Combobox(form,textvariable=fields[name],values=values,state='readonly')
            else:widget=ttk.Entry(form,textvariable=fields[name],width=42)
            widget.grid(row=row,column=1,sticky='ew',padx=8,pady=4)
        ttk.Label(form,text='依据备注').grid(row=len(names),column=0,sticky='nw',pady=4)
        note=tk.Text(form,height=4,width=42)
        note.grid(row=len(names),column=1,sticky='ew',padx=8,pady=4)
        ttk.Label(form,text='这是一条带时间范围的人工声明；完成/失败仍需录像或事件证据复核。',
            wraplength=510).grid(row=len(names)+1,column=0,columnspan=2,sticky='w',pady=8)
        def save():
            try:
                entry=append_task_review(path,**{key:var.get() for key,var in fields.items()},
                    evidence_note=note.get('1.0','end'))
            except Exception as error:
                messagebox.showerror('任务补录失败',str(error));return
            self.write_log('已保存待复核任务声明：'+entry['record_id'])
            dialog.destroy()
        ttk.Button(form,text='保存本段声明',command=save).grid(row=len(names)+2,column=1,sticky='e')
        form.columnconfigure(1,weight=1)

    def open_index(self):
        if self.latest and (Path(self.latest)/'auto_index.html').exists():
            os.startfile(Path(self.latest)/'auto_index.html')
        else:
            self.status.set("尚无最近片段索引；数据菜单可自动整理已有录像")

    def organize_existing(self):
        if self.busy:
            self.status.set("停止并保存后再整理已有录像")
            return
        path = filedialog.askdirectory(title="选择要自动整理的 Episode", initialdir=self.fields['output'].get())
        if not path:
            return
        self.busy = True
        self.start_button.configure(state='disabled')
        self.sync_settings()
        self.status.set('正在自动整理，完整录像保持不变…')
        def run():
            try:
                index = organize(path)
                try:
                    audit = write_episode_sidecar(path)
                    self.messages.put(('compiled', audit['total']))
                except Exception as exc:
                    self.messages.put(('log', '语义候选整理失败；母数据已保存：'+str(exc)))
                self.messages.put(('organized', (str(path), len(index['segments']), len(index['labels']))))
            except Exception as exc:
                self.messages.put(('organize_error', str(exc)))
        threading.Thread(target=run, name='auto-index', daemon=False).start()

    def tick(self):
        while not self.messages.empty():
            kind, text = self.messages.get_nowait()
            if kind == "checked":
                path, quality = text
                self.write_log(f"自动校验完成 · {quality['decoded_frames']}帧 · 问题 {quality['issues']}")
                self.status.set("已保存、自动整理并通过技术校验" if not quality["issues"] else "已保存并自动整理，需要复核；详情在日志中")
                self.last_episode.set("最近录像：" + Path(path).name + " · 数据菜单可打开目录")
            elif kind in {'indexed', 'organized'}:
                path, segments, labels = text
                self.latest = path
                self.write_log(f'自动整理完成 · {segments}段候选 · {labels}条标签 · 数据菜单打开索引')
                self.progress.set(f'已整理 {segments}段候选、{labels}条标签，可在结束后集中复核')
                if kind == 'organized':
                    self.busy = False
                    self.start_button.configure(state='normal')
                    self.sync_settings()
                    self.status.set('自动整理完成 · 数据菜单可打开片段索引')
            elif kind == 'compiled':
                self.write_log(f"语义候选已整理 · {text['units']}个决策单元 · 目标候选{text['goal_candidates']}个；意图仍待核实")
            elif kind == 'update':
                self.write_log(text)
                if not self.busy:
                    self.status.set(text)
            elif kind == 'organize_error':
                self.busy = False
                self.start_button.configure(state='normal')
                self.sync_settings()
                self.status.set('自动整理失败；原数据保留：' + text)
                self.write_log(text)
            else:
                self.write_log(text)
            if kind == "saved":
                self.recording = False
                self.latest = text
                self.stop_button.configure(state="disabled")
                self.status.set("录像已保存，正在自动整理与校验…")
                self.root.deiconify()
            if kind in {"checked", "error", "cancelled"}:
                self.recording = self.busy = False
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.sync_settings()
                self.root.deiconify()
                if kind != "checked":
                    saved = self.collector and self.collector.writer and self.collector.writer.closed
                    prefix = "录像已保存，校验未完成：" if saved else "采集未完成："
                    self.status.set(prefix + text if kind == "error" else text)
            if kind == "log" and self.recording and self.collector and not self.collector.writer:
                self.status.set(text)
        if self.recording and self.collector and self.collector.writer:
            seconds = int(self.collector.writer.now()/1e9)
            self.status.set("正在采集 · 游戏保持前台；F10停止并保存")
            self.progress.set(f"采集时长 {seconds//60:02d}:{seconds%60:02d}")
            frame = self.collector.latest_preview
            if frame is not None:
                image = Image.fromarray(frame[:, :, ::-1])
                image.thumbnail((780, 270))
                self.photo = ImageTk.PhotoImage(image)
                self.preview.configure(image=self.photo, text="", height=270)
        self.root.after(200, self.tick)

    def exit(self):
        if self.busy:
            self.stop()
            self.root.after(300, self.exit)
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()
