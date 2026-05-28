#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows S3/S4 循环唤醒测试工具 v1.4 (EXE 路径修复版)
- 修复: InstanceId 日志显示
- 修复: 子线程读取 tkinter 变量（拷贝配置）
- 修复: 全设备快照对比影响最终 PASS/FAIL
- 修复: save_state 线程安全
- 修复: 文件路径识别 exe 所在目录（兼容 PyInstaller）
- 修复: lost/new/changes 共享引用
- 修复: 关闭窗口后 root.after 崩溃
- 增强: 入睡前设备状态写入日志
- 增强: Problem 字段类型转换保护
- 默认目标次数: 1000
"""

import sys
import os
import json
import time
import ctypes
import subprocess
import logging
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

# -------------------- 路径修复：支持 PyInstaller 打包 --------------------
if getattr(sys, 'frozen', False):
    # 打包成 exe 后，程序根目录是 exe 所在的文件夹
    SCRIPT_DIR = Path(sys.executable).parent
else:
    # 正常 Python 脚本运行，根目录是脚本所在文件夹
    SCRIPT_DIR = Path(__file__).resolve().parent

LOG_DIR = SCRIPT_DIR / "Logs"
LOG_DIR.mkdir(exist_ok=True)

# -------------------- 管理员提权 --------------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    if sys.argv[0].endswith('.py'):
        app_path = sys.executable
        script = os.path.abspath(sys.argv[0])
        extra_args = ' '.join(f'"{arg}"' for arg in sys.argv[1:])
        params = f'"{script}" {extra_args}'
    else:
        app_path = os.path.abspath(sys.argv[0])
        params = ' '.join(f'"{arg}"' for arg in sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", app_path, params, None, 1)
    sys.exit()

if not is_admin():
    run_as_admin()

# -------------------- 工具函数 --------------------
def execute_powershell(command, timeout=30):
    prefix = (
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    )
    full_cmd = prefix + command
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", full_cmd],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace'
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"PowerShell 命令超时 ({timeout}秒)")

def get_all_devices_snapshot():
    cmd = "Get-PnpDevice -PresentOnly | Select-Object FriendlyName, InstanceId, Class, Status, Problem | ConvertTo-Json"
    data = execute_powershell(cmd, timeout=30)
    devices = json.loads(data)
    if isinstance(devices, dict):
        devices = [devices]
    snapshot = {}
    skipped = 0
    for dev in devices:
        iid = dev.get('InstanceId')
        if not iid:
            skipped += 1
            continue
        problem_val = dev.get('Problem')
        try:
            problem_val = int(problem_val) if problem_val is not None else 0
        except (ValueError, TypeError):
            problem_val = 0
        snapshot[iid] = {
            'FriendlyName': dev.get('FriendlyName', ''),
            'Class': dev.get('Class', ''),
            'Status': dev.get('Status', 'Unknown'),
            'Problem': problem_val
        }
    return snapshot, skipped

def check_specified_devices(device_names, current_snapshot):
    details = []
    all_ok = True
    for name in device_names:
        matches = [d for d in current_snapshot.values() if d['FriendlyName'] == name]
        if not matches:
            details.append((name, "缺失", False))
            all_ok = False
        else:
            normal = any(m['Status'] == 'OK' and m['Problem'] == 0 for m in matches)
            if normal:
                details.append((name, "存在且正常", True))
            else:
                status_info = "; ".join([f"Status={m['Status']}, Problem={m['Problem']}" for m in matches])
                details.append((name, f"存在但驱动异常 ({status_info})", False))
                all_ok = False
    return all_ok, details

def compare_snapshots(before, after, ignore_status_change=False):
    """对比快照，返回 lost, new, changes。每个设备字典包含 InstanceId"""
    lost = [{**before[i], 'InstanceId': i} for i in (set(before) - set(after))]
    new  = [{**after[i], 'InstanceId': i} for i in (set(after) - set(before))]
    changes = []
    if not ignore_status_change:
        for i in set(before) & set(after):
            b, a = before[i], after[i]
            if b['Status'] != a['Status'] or b['Problem'] != a['Problem']:
                if a['Status'] != 'OK' or a['Problem'] != 0:
                    changes.append({
                        **a,
                        'InstanceId': i,
                        'BeforeStatus': b['Status'], 'BeforeProblem': b['Problem'],
                        'AfterStatus': a['Status'], 'AfterProblem': a['Problem']
                    })
    return lost, new, changes

def set_wake_timer(seconds):
    hTimer = ctypes.windll.kernel32.CreateWaitableTimerW(None, True, None)
    due = ctypes.c_longlong(-int(seconds * 10_000_000))
    if not ctypes.windll.kernel32.SetWaitableTimer(hTimer, ctypes.byref(due), 0, None, None, True):
        ctypes.windll.kernel32.CloseHandle(hTimer)
        raise OSError("SetWaitableTimer 失败")
    return hTimer

def cancel_wake_timer(hTimer):
    if hTimer:
        ctypes.windll.kernel32.CancelWaitableTimer(hTimer)
        ctypes.windll.kernel32.CloseHandle(hTimer)

def sleep_system(wake_seconds=None):
    hTimer = set_wake_timer(wake_seconds) if wake_seconds is not None else None
    try:
        if ctypes.windll.powrprof.SetSuspendState(0, 1, 0) == 0:
            raise OSError(f"SetSuspendState 失败, 错误码: {ctypes.get_last_error()}")
    finally:
        cancel_wake_timer(hTimer)

def hibernate_system(wake_seconds=None):
    hTimer = set_wake_timer(wake_seconds) if wake_seconds is not None else None
    try:
        if ctypes.windll.powrprof.SetSuspendState(1, 1, 0) == 0:
            raise OSError(f"休眠失败, 错误码: {ctypes.get_last_error()}")
    finally:
        cancel_wake_timer(hTimer)

# -------------------- 日志设置 --------------------
def setup_logging(log_path=None):
    logger = logging.getLogger("S3S4Test")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    if log_path is None:
        log_path = LOG_DIR / f"s3s4_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(fh)
    return logger, log_path

# -------------------- 主 GUI 应用 --------------------
class S3S4TestApp:
    def __init__(self, root):
        self.root = root
        self.root.title("S3/S4 循环唤醒测试工具 v1.4")
        self.root.geometry("850x650")
        self.root.minsize(750, 550)

        self.test_type_var = tk.StringVar(value="S3")
        self.test_running = False
        self.stop_requested = False
        self.logger = None
        self.log_file = None
        self.test_thread = None
        self.state_file = SCRIPT_DIR / "s3s4_state.json"
        self.current_cycle = 0
        self.target_cycles = 1000      # 默认 1000 次
        self.all_ok_so_far = True
        self.device_list = []

        self.enable_snapshot = tk.BooleanVar(value=True)
        self.loss_as_fail = tk.BooleanVar(value=True)
        self.ignore_status_change = tk.BooleanVar(value=False)
        self.log_new_device = tk.BooleanVar(value=True)
        self.fuzzy_match = tk.BooleanVar(value=False)

        self.wake_mode = tk.StringVar(value="timer")
        self.wake_timer_sec = 30
        self.pre_sleep_wait = 15
        self.post_wake_wait = 15

        self.create_widgets()
        self.load_state()
        self.root.after(100, self.load_default_device_list)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if "--auto" in sys.argv:
            self.root.after(1000, self.start_test)

    def create_widgets(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        sf = ttk.LabelFrame(main, text="测试状态", padding=5)
        sf.pack(fill=tk.X, pady=(0,5))
        self.status_label = ttk.Label(sf, text="未开始", font=("Arial", 14, "bold"))
        self.status_label.grid(row=0, column=0, sticky=tk.W)
        self.progress_label = ttk.Label(sf, text="0 / 0", font=("Courier New", 32, "bold"))
        self.progress_label.grid(row=1, column=0, sticky=tk.W, pady=5)
        self.countdown_label = ttk.Label(sf, text="", font=("Arial", 18), foreground="red")
        self.countdown_label.grid(row=2, column=0, sticky=tk.W)

        mf = ttk.LabelFrame(main, text="测试模式", padding=5)
        mf.pack(fill=tk.X, pady=5)
        ttk.Radiobutton(mf, text="S3 (睡眠)", variable=self.test_type_var, value="S3").grid(row=0, column=0, padx=5)
        ttk.Radiobutton(mf, text="S4 (休眠)", variable=self.test_type_var, value="S4").grid(row=0, column=1, padx=5)
        ttk.Radiobutton(mf, text="混合", variable=self.test_type_var, value="Mixed").grid(row=0, column=2, padx=5)

        ttk.Label(mf, text="目标次数 (0=无限):").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.target_entry = ttk.Entry(mf, width=8); self.target_entry.grid(row=1, column=1, sticky=tk.W); self.target_entry.insert(0, "1000")
        ttk.Label(mf, text="进入前等待(秒):").grid(row=1, column=2, sticky=tk.W, padx=5)
        self.pre_wait_entry = ttk.Entry(mf, width=6); self.pre_wait_entry.grid(row=1, column=3, sticky=tk.W); self.pre_wait_entry.insert(0, "15")
        ttk.Label(mf, text="唤醒后等待(秒):").grid(row=1, column=4, sticky=tk.W, padx=5)
        self.post_wait_entry = ttk.Entry(mf, width=6); self.post_wait_entry.grid(row=1, column=5, sticky=tk.W); self.post_wait_entry.insert(0, "15")
        ttk.Label(mf, text="唤醒方式:").grid(row=2, column=0, sticky=tk.W, padx=5)
        ttk.Radiobutton(mf, text="定时唤醒", variable=self.wake_mode, value="timer").grid(row=2, column=1, padx=5)
        ttk.Radiobutton(mf, text="手动唤醒", variable=self.wake_mode, value="manual").grid(row=2, column=2, padx=5)
        ttk.Label(mf, text="定时秒数:").grid(row=2, column=3, sticky=tk.W, padx=5)
        self.timer_entry = ttk.Entry(mf, width=6); self.timer_entry.grid(row=2, column=4, sticky=tk.W); self.timer_entry.insert(0, "30")

        df = ttk.LabelFrame(main, text="监控设备列表 (友好名称，每行一个)", padding=5)
        df.pack(fill=tk.BOTH, expand=True, pady=5)
        self.device_text = scrolledtext.ScrolledText(df, height=6)
        self.device_text.pack(fill=tk.BOTH, expand=True)
        dbtn = ttk.Frame(df); dbtn.pack(fill=tk.X, pady=5)
        self.import_btn = ttk.Button(dbtn, text="从设备管理器导入", command=self.import_devices)
        self.import_btn.pack(side=tk.LEFT, padx=5)
        self.save_btn = ttk.Button(dbtn, text="保存列表", command=self.save_devices)
        self.save_btn.pack(side=tk.LEFT, padx=5)
        self.load_btn = ttk.Button(dbtn, text="加载列表", command=self.load_devices)
        self.load_btn.pack(side=tk.LEFT, padx=5)

        af = ttk.LabelFrame(main, text="高级选项", padding=5)
        af.pack(fill=tk.X, pady=5)
        self.adv_visible = tk.BooleanVar(value=True)
        ttk.Checkbutton(af, text="展开/收起", variable=self.adv_visible, command=self.toggle_adv).pack(anchor=tk.W)
        self.adv_inner = ttk.Frame(af); self.adv_inner.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(self.adv_inner, text="启用全设备快照对比", variable=self.enable_snapshot).grid(row=0, column=0, sticky=tk.W, padx=10)
        ttk.Checkbutton(self.adv_inner, text="设备丢失判失败", variable=self.loss_as_fail).grid(row=0, column=1, sticky=tk.W, padx=10)
        ttk.Checkbutton(self.adv_inner, text="忽略状态变化异常", variable=self.ignore_status_change).grid(row=1, column=0, sticky=tk.W, padx=10)
        ttk.Checkbutton(self.adv_inner, text="记录新增设备", variable=self.log_new_device).grid(row=1, column=1, sticky=tk.W, padx=10)

        ctrl = ttk.Frame(main); ctrl.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(ctrl, text="开始测试", command=self.start_test)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(ctrl, text="停止", command=self.stop_test, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.reset_btn = ttk.Button(ctrl, text="重置计数", command=self.reset_counter)
        self.reset_btn.pack(side=tk.LEFT, padx=5)

        bot = ttk.Frame(main); bot.pack(fill=tk.X, pady=5)
        ttk.Button(bot, text="打开日志目录", command=lambda: os.startfile(LOG_DIR)).pack(side=tk.LEFT, padx=5)

    def toggle_adv(self):
        if self.adv_visible.get():
            self.adv_inner.pack(fill=tk.X, pady=2)
        else:
            self.adv_inner.pack_forget()

    def load_default_device_list(self):
        default_file = SCRIPT_DIR / "device_list.txt"
        if default_file.exists():
            current = self.device_text.get(1.0, tk.END).strip()
            if not current:
                try:
                    content = default_file.read_text(encoding='utf-8').strip()
                    if content:
                        self.device_text.insert(tk.END, content)
                except Exception:
                    pass

    def import_devices(self):
        try:
            snap, _ = get_all_devices_snapshot()
            all_names = sorted(set(d['FriendlyName'] for d in snap.values() if d['FriendlyName']))
            if not all_names:
                messagebox.showinfo("提示", "未找到任何设备")
                return

            select_win = tk.Toplevel(self.root)
            select_win.title("选择要监控的设备")
            select_win.geometry("600x500")
            select_win.transient(self.root)
            select_win.grab_set()

            search_frame = ttk.Frame(select_win)
            search_frame.pack(fill=tk.X, padx=10, pady=5)
            ttk.Label(search_frame, text="搜索关键词:").pack(side=tk.LEFT)
            search_var = tk.StringVar()
            search_entry = ttk.Entry(search_frame, textvariable=search_var)
            search_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

            list_frame = ttk.Frame(select_win)
            list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

            scrollbar = ttk.Scrollbar(list_frame)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            listbox = tk.Listbox(list_frame, selectmode=tk.MULTIPLE, yscrollcommand=scrollbar.set)
            listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.config(command=listbox.yview)

            for name in all_names:
                listbox.insert(tk.END, name)

            def filter_list(*args):
                keyword = search_var.get().lower()
                listbox.delete(0, tk.END)
                for name in all_names:
                    if keyword in name.lower():
                        listbox.insert(tk.END, name)

            search_var.trace_add("write", filter_list)

            btn_frame = ttk.Frame(select_win)
            btn_frame.pack(pady=10)

            def on_confirm():
                selected_indices = listbox.curselection()
                if not selected_indices:
                    messagebox.showwarning("警告", "未选择任何设备", parent=select_win)
                    return
                selected_names = [listbox.get(i) for i in selected_indices]

                current_text = self.device_text.get(1.0, tk.END).strip()
                existing = set(line.strip() for line in current_text.splitlines() if line.strip())
                to_add = [name for name in selected_names if name not in existing]
                if to_add:
                    if current_text:
                        self.device_text.insert(tk.END, "\n" + "\n".join(to_add))
                    else:
                        self.device_text.insert(tk.END, "\n".join(to_add))
                else:
                    messagebox.showinfo("提示", "所选设备已全部在列表中", parent=select_win)
                select_win.destroy()

            ttk.Button(btn_frame, text="确定", command=on_confirm).pack(side=tk.LEFT, padx=10)
            ttk.Button(btn_frame, text="取消", command=select_win.destroy).pack(side=tk.LEFT, padx=10)

        except Exception as e:
            messagebox.showerror("错误", f"导入失败: {e}")

    def save_devices(self):
        try:
            content = self.device_text.get(1.0, tk.END).strip()
            path = SCRIPT_DIR / "device_list.txt"
            path.write_text(content, encoding='utf-8')
            messagebox.showinfo("成功", f"已保存到 {path.absolute()}")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败: {e}")

    def load_devices(self):
        path = filedialog.askopenfilename(filetypes=[("Text", "*.txt")])
        if path:
            self.device_text.delete(1.0, tk.END)
            self.device_text.insert(tk.END, Path(path).read_text(encoding='utf-8'))

    def load_state(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding='utf-8'))
                self.current_cycle = data.get("current_cycle", 0)
                self.target_cycles = data.get("target_cycles", 1000)
                self.all_ok_so_far = data.get("all_ok_so_far", True)
                self.test_type_var.set(data.get("test_type", "S3"))
                self.progress_label.config(text=f"{self.current_cycle} / {self.target_cycles}")
            except: pass

    def save_state(self):
        # 使用拷贝的 _test_type，避免子线程访问 tkinter 变量
        state = {
            "current_cycle": self.current_cycle,
            "target_cycles": self.target_cycles,
            "all_ok_so_far": self.all_ok_so_far,
            "test_type": getattr(self, '_test_type', self.test_type_var.get()),
            "log_file": str(self.log_file) if self.log_file else "",
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.state_file.write_text(json.dumps(state, indent=2), encoding='utf-8')

    def reset_counter(self):
        if messagebox.askyesno("确认", "确定重置进度？"):
            self.current_cycle = 0
            self.all_ok_so_far = True
            self.state_file.unlink(missing_ok=True)
            self.progress_label.config(text="0 / 0")

    def safe_after(self, func):
        """安全调用 root.after，避免窗口销毁后出错"""
        try:
            if self.root.winfo_exists():
                self.root.after(0, func)
        except tk.TclError:
            pass

    def start_test(self):
        if self.test_running:
            return
        try:
            self.target_cycles = int(self.target_entry.get())
            self.pre_sleep_wait = int(self.pre_wait_entry.get())
            self.post_wake_wait = int(self.post_wait_entry.get())
            self.wake_timer_sec = int(self.timer_entry.get())
        except:
            messagebox.showerror("输入错误", "请填写有效数字")
            return

        # 拷贝所有 tkinter 变量，子线程只读这些副本
        self._test_type = self.test_type_var.get()
        self._wake_mode = self.wake_mode.get()
        self._wake_timer_sec = self.wake_timer_sec
        self._enable_snapshot = self.enable_snapshot.get()
        self._loss_as_fail = self.loss_as_fail.get()
        self._ignore_status_change = self.ignore_status_change.get()
        self._log_new_device = self.log_new_device.get()
        self._device_list = [l.strip() for l in self.device_text.get(1.0, tk.END).splitlines() if l.strip()]
        self._pre_sleep_wait = self.pre_sleep_wait
        self._post_wake_wait = self.post_wake_wait
        self._target_cycles = self.target_cycles

        self.logger, self.log_file = setup_logging()
        self.logger.info("==================================================")
        self.logger.info(f"S3/S4 循环唤醒测试开始 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"类型: {self._test_type}  目标: {self._target_cycles if self._target_cycles else '无限'}")
        self.save_state()

        # 禁用界面相关控件
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.reset_btn.config(state=tk.DISABLED)
        self.import_btn.config(state=tk.DISABLED)
        self.save_btn.config(state=tk.DISABLED)
        self.load_btn.config(state=tk.DISABLED)
        self.device_text.config(state=tk.DISABLED)

        self.test_running = True
        self.stop_requested = False
        self.test_thread = threading.Thread(target=self.run_loop, daemon=True)
        self.test_thread.start()

    def stop_test(self):
        self.stop_requested = True

    def on_close(self):
        if self.test_running:
            if not messagebox.askyesno("退出", "测试运行中，确定退出？"):
                return
            self.stop_test()
            if self.test_thread and self.test_thread.is_alive():
                self.test_thread.join(timeout=5.0)
        self.root.destroy()

    def run_loop(self):
        try:
            while not self.stop_requested:
                if self._target_cycles > 0 and self.current_cycle >= self._target_cycles:
                    break
                cycle = self.current_cycle + 1
                self.safe_after(lambda: self.status_label.config(text=f"周期 {cycle} 准备中..."))
                mode = self._test_type if self._test_type != "Mixed" else ("S3" if cycle % 2 else "S4")

                # 入睡前快照
                try:
                    before_snap, _ = get_all_devices_snapshot()
                except Exception as e:
                    self.logger.error(f"获取进入前快照失败: {e}")
                    self.all_ok_so_far = False
                    self.current_cycle = cycle
                    self.save_state()
                    continue

                before_ok, before_details = check_specified_devices(self._device_list, before_snap)
                for _, _, ok in before_details:
                    if not ok:
                        self.all_ok_so_far = False

                # 倒计时
                for i in range(self._pre_sleep_wait, 0, -1):
                    if self.stop_requested:
                        break
                    self.safe_after(lambda v=i: self.countdown_label.config(text=f"进入 {mode} 倒计时: {v} 秒"))
                    time.sleep(1)
                self.safe_after(lambda: self.countdown_label.config(text=""))
                if self.stop_requested:
                    break

                entry_time = datetime.now()
                self.safe_after(lambda: self.status_label.config(text=f"进入 {mode} ..."))
                wake_sec = self._wake_timer_sec if self._wake_mode == "timer" else None
                try:
                    if mode == "S3":
                        sleep_system(wake_seconds=wake_sec)
                    else:
                        hibernate_system(wake_seconds=wake_sec)
                except Exception as e:
                    self.logger.error(f"执行 {mode} 失败: {e}")
                    self.all_ok_so_far = False
                    self.current_cycle = cycle
                    self.save_state()
                    continue

                wake_time = datetime.now()
                sleep_sec = round((wake_time - entry_time).total_seconds(), 1)
                self.safe_after(lambda: self.status_label.config(text="系统已唤醒，等待恢复..."))
                time.sleep(self._post_wake_wait)

                # 唤醒后快照
                try:
                    after_snap, _ = get_all_devices_snapshot()
                except Exception as e:
                    self.logger.error(f"获取唤醒后快照失败: {e}")
                    self.all_ok_so_far = False
                    self.current_cycle = cycle
                    self.save_state()
                    continue

                after_ok, after_details = check_specified_devices(self._device_list, after_snap)
                if not after_ok:
                    self.all_ok_so_far = False

                lost, new, changes = [], [], []  # 修复共享引用
                if self._enable_snapshot:
                    lost, new, changes = compare_snapshots(before_snap, after_snap, self._ignore_status_change)

                cycle_fail = False
                # 全设备快照对比结果更新 all_ok_so_far
                if lost and self._loss_as_fail:
                    cycle_fail = True
                    self.all_ok_so_far = False
                if changes and not self._ignore_status_change:
                    cycle_fail = True
                    self.all_ok_so_far = False
                if not after_ok:
                    cycle_fail = True

                # 构建日志块
                block = []
                block.append("")
                block.append(f"========== 第 {cycle} 次 {mode} 唤醒 ==========")
                block.append(f"进入 {mode} 时间: {entry_time.strftime('%Y-%m-%d %H:%M:%S')}")
                block.append(f"唤醒时间: {wake_time.strftime('%Y-%m-%d %H:%M:%S')} (睡眠约 {sleep_sec} 秒)")
                block.append(f"本次检查是否有异常: {'是' if cycle_fail else '否'}")
                block.append("入睡前指定设备状态:")
                for name, status_str, _ in before_details:
                    block.append(f"  - {name}: {status_str}")
                block.append("唤醒后指定设备状态:")
                for name, status_str, _ in after_details:
                    block.append(f"  - {name}: {status_str}")
                if self._enable_snapshot:
                    block.append("全设备快照对比结果:")
                    if lost:
                        block.append(f" 丢失设备 ({len(lost)}个):")
                        for d in lost:
                            block.append(f"  - {d['FriendlyName']} ({d['InstanceId']})")
                    else:
                        block.append(" 丢失设备: 无")
                    if new and self._log_new_device:
                        block.append(f" 新增设备 ({len(new)}个):")
                        for d in new:
                            block.append(f"  + {d['FriendlyName']} ({d['InstanceId']})")
                    else:
                        block.append(" 新增设备: 无")
                    if changes:
                        block.append(f" 状态变化 ({len(changes)}个):")
                        for ch in changes:
                            block.append(f"  * {ch['FriendlyName']} ({ch['InstanceId']}): "
                                         f"Status {ch['BeforeStatus']}→{ch['AfterStatus']}, Problem {ch['BeforeProblem']}→{ch['AfterProblem']}")
                    else:
                        block.append(" 状态变化: 无")
                block.append("=================================\n")
                self.logger.info("\n".join(block))

                self.current_cycle = cycle
                self.safe_after(lambda: self.progress_label.config(
                    text=f"{self.current_cycle} / {self._target_cycles if self._target_cycles else '∞'}"))
                self.save_state()

            self.finish_test()
        except Exception:
            self.logger.exception("测试线程异常退出")
            self.all_ok_so_far = False
            self.finish_test()

    def finish_test(self):
        self.test_running = False
        result = "PASS" if self.all_ok_so_far else "FAIL"
        self.logger.info("\n==================================================")
        self.logger.info("========== 测试终止 ==========")
        self.logger.info(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"总共完成唤醒次数: {self.current_cycle}")
        if self._target_cycles > 0:
            self.logger.info(f"目标次数: {self._target_cycles}")
        self.logger.info(f"最终结果: {'通过（所有检查均正常）' if result == 'PASS' else '失败（存在异常周期，详见日志）'}")
        self.logger.info("=================================\n")
        self.save_state()
        self.safe_after(lambda: self._on_test_finished(result))

    def _on_test_finished(self, result):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.reset_btn.config(state=tk.NORMAL)
        self.import_btn.config(state=tk.NORMAL)
        self.save_btn.config(state=tk.NORMAL)
        self.load_btn.config(state=tk.NORMAL)
        self.device_text.config(state=tk.NORMAL)

        if result == "PASS":
            self.status_label.config(text="PASS", foreground="green")
            title, msg = "测试通过", "所有周期均无异常。"
        else:
            self.status_label.config(text="FAIL", foreground="red")
            title, msg = "测试失败", "存在异常周期，详见日志。"
        if messagebox.askyesno(title, f"{msg}\n是否打开日志文件？"):
            if self.log_file and self.log_file.exists():
                os.startfile(self.log_file)


if __name__ == "__main__":
    root = tk.Tk()
    app = S3S4TestApp(root)
    root.mainloop()
