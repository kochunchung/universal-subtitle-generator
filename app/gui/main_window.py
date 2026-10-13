"""主視窗:左邊是佇列與日誌,右邊是顯存監控與處理選項。"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue as queue_mod
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app import settings as settings_mod
from app.core.media import AUDIO_EXTS, VIDEO_EXTS
from app.gpu.monitor import GpuMonitor
from app.paths import ROOT

APP_TITLE = "萬能字幕產生器"
SUPPORTED = VIDEO_EXTS | AUDIO_EXTS | {".srt"}

STATE_TEXT = {
    "queued": "等待中", "running": "處理中", "transcribed": "轉錄完成", "done": "完成",
    "skipped": "略過", "no_speech": "沒有人聲", "failed": "失敗", "canceled": "已取消",
}
STATE_COLOR = {"done": "#2e7d32", "failed": "#c62828", "canceled": "#b26a00", "skipped": "#757575",
               "no_speech": "#757575"}

SOURCE_LANGS = [("自動偵測", "auto"), ("中文", "zh"), ("英文", "en"), ("日文", "ja"), ("韓文", "ko")]
TARGETS = [("不翻譯", None), ("繁體中文", "zh-TW"), ("英文", "en"), ("日文", "ja")]
MODES = [("只有譯文", "translated"), ("雙語(上原文、下譯文)", "bilingual")]
MODEL_WEIGHT_MB = {"openai/whisper-large-v3-turbo": 1600, "openai/whisper-large-v3": 3100,
                   "openai/whisper-medium": 1500}


@dataclass
class Job:
    path: Path
    kind: str                      # media / srt
    streams: list[str] = field(default_factory=list)
    stream: int = 0
    state: str = "queued"
    progress: float = 0.0
    message: str = ""
    outputs: dict = field(default_factory=dict)


class QueueTable(QTableWidget):
    """可以把檔案或資料夾拖進來的佇列表格。"""

    def __init__(self, on_files) -> None:
        super().__init__(0, 4)
        self.on_files = on_files
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setHorizontalHeaderLabels(["檔名", "音軌", "狀態", "進度"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(2, 260)
        self.setColumnWidth(3, 130)

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        self.on_files(paths)

    def paintEvent(self, e):  # noqa: N802
        super().paintEvent(e)
        if self.rowCount() == 0:
            from PySide6.QtGui import QPainter

            p = QPainter(self.viewport())
            p.setPen(QColor("#9e9e9e"))
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                       "把影片、音檔或 SRT 字幕拖到這裡\n(也可以拖整個資料夾)")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 760)
        self.settings = settings_mod.load()
        self.jobs: list[Job] = []
        self.proc: mp.Process | None = None
        self.q = None
        self.started_at = 0.0
        self.got_done = False

        central = QWidget()
        root = QVBoxLayout(central)

        # ---- 工具列
        bar = QHBoxLayout()
        self.btn_add = QPushButton("加入檔案")
        self.btn_start = QPushButton("開始處理")
        self.btn_cancel = QPushButton("取消")
        self.btn_remove = QPushButton("移除選取")
        self.btn_clear = QPushButton("清除已完成")
        self.btn_env = QPushButton("環境檢查")
        self.btn_gloss = QPushButton("詞彙表")
        self.btn_settings = QPushButton("設定(API 金鑰)")
        self.btn_start.setStyleSheet("font-weight: 600;")
        for b in (self.btn_add, self.btn_start, self.btn_cancel, self.btn_remove, self.btn_clear):
            bar.addWidget(b)
        bar.addStretch(1)
        for b in (self.btn_env, self.btn_gloss, self.btn_settings):
            bar.addWidget(b)
        root.addLayout(bar)

        # ---- 左:佇列 + 日誌;右:顯存 + 選項
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.table = QueueTable(self.add_files)
        ll.addWidget(self.table, 3)
        ll.addWidget(QLabel("處理日誌"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        ll.addWidget(self.log, 1)
        bottom = QHBoxLayout()
        self.btn_preview = QPushButton("預覽字幕")
        self.btn_folder = QPushButton("開啟資料夾")
        self.status = QLabel("")
        bottom.addWidget(self.btn_preview)
        bottom.addWidget(self.btn_folder)
        bottom.addStretch(1)
        bottom.addWidget(self.status)
        ll.addLayout(bottom)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self._build_gpu_panel())
        rl.addWidget(self._build_options())
        rl.addStretch(1)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        self.setCentralWidget(central)

        # ---- 事件
        self.btn_add.clicked.connect(self.pick_files)
        self.btn_start.clicked.connect(lambda: self.start())
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear_done)
        self.btn_env.clicked.connect(self.env_check)
        self.btn_gloss.clicked.connect(self.edit_glossary)
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_preview.clicked.connect(self.preview)
        self.btn_folder.clicked.connect(self.open_folder)
        self.table.itemDoubleClicked.connect(lambda *_: self.preview())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_row_menu)

        self.monitor = GpuMonitor()
        self.gpu_timer = QTimer(self)
        self.gpu_timer.timeout.connect(self.refresh_gpu)
        self.gpu_timer.start(1000)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.refresh_gpu()
        self.update_buttons()
        self.append_log(f"程式資料夾:{ROOT}")

    # ================================================================== 面板
    def _build_gpu_panel(self) -> QGroupBox:
        g = QGroupBox("顯存監控")
        lay = QVBoxLayout(g)
        self.gpu_name = QLabel("讀取中…")
        self.gpu_bar = QProgressBar()
        self.gpu_bar.setRange(0, 1000)
        self.gpu_bar.setTextVisible(True)
        self.gpu_temp = QLabel("")
        self.gpu_batch = QLabel("")
        self.gpu_batch.setWordWrap(True)
        for w in (self.gpu_name, self.gpu_bar, self.gpu_temp, self.gpu_batch):
            lay.addWidget(w)
        return g

    def _build_options(self) -> QGroupBox:
        g = QGroupBox("處理選項")
        f = QFormLayout(g)
        s = self.settings
        self.cb_model = QComboBox()
        for label, repo in settings_mod.WHISPER_MODELS.items():
            self.cb_model.addItem(label, repo)
        self._select(self.cb_model, s.get("whisper_model"))
        self.cb_src = QComboBox()
        for label, v in SOURCE_LANGS:
            self.cb_src.addItem(label, v)
        self._select(self.cb_src, s.get("source_language") or "auto")
        self.cb_tgt = QComboBox()
        for label, v in TARGETS:
            self.cb_tgt.addItem(label, v)
        self._select(self.cb_tgt, s.get("translate_to"))
        self.cb_mode = QComboBox()
        for label, v in MODES:
            self.cb_mode.addItem(label, v)
        self._select(self.cb_mode, s.get("subtitle_mode", "translated"))
        self.cb_engine = QComboBox()
        for v, label in settings_mod.TRANSLATION_ENGINES.items():
            self.cb_engine.addItem(label, v)
        self._select(self.cb_engine, s.get("translation_engine", "m2m100"))
        self.chk_opencc = QCheckBox("中文轉台灣繁體")
        self.chk_opencc.setChecked(s.get("opencc_s2twp", True))
        self.chk_vad = QCheckBox("VAD 過濾靜音(減少幻覺)")
        self.chk_vad.setChecked(s.get("vad", True))
        self.chk_del = QCheckBox("完成後刪除 MP3")
        self.chk_del.setChecked(s.get("delete_mp3", False))
        self.chk_txt = QCheckBox("另外輸出 TXT")
        self.chk_txt.setChecked(s.get("write_txt", False))
        f.addRow("轉錄模型", self.cb_model)
        f.addRow("原文語言", self.cb_src)
        f.addRow("翻譯目標", self.cb_tgt)
        f.addRow("字幕輸出", self.cb_mode)
        f.addRow("翻譯引擎", self.cb_engine)
        for c in (self.chk_opencc, self.chk_vad, self.chk_del, self.chk_txt):
            f.addRow(c)
        for w in (self.cb_model, self.cb_src, self.cb_tgt, self.cb_mode, self.cb_engine):
            w.currentIndexChanged.connect(self.save_options)
        for c in (self.chk_opencc, self.chk_vad, self.chk_del, self.chk_txt):
            c.toggled.connect(self.save_options)
        self.cb_tgt.currentIndexChanged.connect(self._toggle_translate_opts)
        self._toggle_translate_opts()
        return g

    @staticmethod
    def _select(cb: QComboBox, value) -> None:
        i = cb.findData(value)
        if i >= 0:
            cb.setCurrentIndex(i)

    def _toggle_translate_opts(self) -> None:
        on = self.cb_tgt.currentData() is not None
        self.cb_mode.setEnabled(on)
        self.cb_engine.setEnabled(on)

    def save_options(self) -> None:
        s = self.settings
        s["whisper_model"] = self.cb_model.currentData()
        s["source_language"] = self.cb_src.currentData()
        s["translate_to"] = self.cb_tgt.currentData()
        s["subtitle_mode"] = self.cb_mode.currentData()
        s["translation_engine"] = self.cb_engine.currentData()
        s["opencc_s2twp"] = self.chk_opencc.isChecked()
        s["vad"] = self.chk_vad.isChecked()
        s["delete_mp3"] = self.chk_del.isChecked()
        s["write_txt"] = self.chk_txt.isChecked()
        settings_mod.save(s)
        self.refresh_gpu()

    # ================================================================== 顯存
    def refresh_gpu(self) -> None:
        st = self.monitor.read()
        if st is None:
            self.gpu_name.setText("無法讀取顯示卡資訊(NVML)")
            return
        self.gpu_name.setText(f"{st.name} · {st.mem_total_mb / 1024:.0f} GB · 驅動 {st.driver}")
        self.gpu_bar.setValue(int(st.mem_used_mb / max(1, st.mem_total_mb) * 1000))
        self.gpu_bar.setFormat(f"已用 {st.mem_used_mb / 1024:.1f} / {st.mem_total_mb / 1024:.1f} GB")
        temp = f"{st.temperature_c}°C" if st.temperature_c is not None else "—"
        util = f"{st.utilization_pct}%" if st.utilization_pct is not None else "—"
        self.gpu_temp.setText(f"溫度 {temp} · 使用率 {util}")
        if self.proc is not None:
            self.gpu_batch.setText("處理中(已用量包含字幕產生器)")
            return
        model = self.settings.get("whisper_model")
        cal = (self.settings.get("calibration") or {}).get(model) or {}
        if self.settings.get("batch_size"):
            self.gpu_batch.setText(f"批次大小:固定 {self.settings['batch_size']}(設定中指定)")
        elif cal:
            free = st.mem_free_mb - MODEL_WEIGHT_MB.get(model, 1600) - 700
            bs = int(max(1, min(16, free // cal["per_item_mb"])))
            self.gpu_batch.setText(f"建議批次:{bs}(依目前剩餘顯存與校準數據,開始處理時會再確認)")
        else:
            self.gpu_batch.setText("此模型尚未校準顯存,先用保守批次;可執行 calibrate-vram.bat 取得最佳值")

    # ================================================================== 佇列
    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED))
        files, _ = QFileDialog.getOpenFileNames(self, "選擇影片、音檔或字幕", "", f"支援的檔案 ({exts});;所有檔案 (*)")
        self.add_files(files)

    def add_files(self, paths: list[str]) -> None:
        from app.core import history, media

        found: list[Path] = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                found += sorted(x for x in pp.rglob("*") if x.suffix.lower() in SUPPORTED)
            elif pp.suffix.lower() in SUPPORTED:
                found.append(pp)
        existing = {j.path.resolve() for j in self.jobs}
        added = 0
        for p in found:
            if p.resolve() in existing:
                continue
            kind = "srt" if p.suffix.lower() == ".srt" else "media"
            job = Job(path=p, kind=kind)
            if kind == "media":
                try:
                    info = media.probe(p)
                    job.streams = [s.label() for s in info.audio_streams]
                    srt = history.find_transcript(p)
                    if srt is not None:   # 以前轉錄過:直接標成完成,之後可以只翻譯
                        job.outputs["srt"] = str(srt)
                        job.state, job.progress, job.message = "done", 1.0, f"已有字幕 {srt.name}"
                except Exception as e:  # noqa: BLE001
                    job.state, job.message = "failed", f"無法讀取:{e}"
            self.jobs.append(job)
            self._add_row(job)
            added += 1
        if added:
            self.append_log(f"加入 {added} 個檔案")
        elif paths:
            self.append_log("沒有可加入的檔案(可能重複,或格式不支援)")
        self.update_buttons()

    def _add_row(self, job: Job) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        name = QTableWidgetItem(job.path.name)
        name.setToolTip(str(job.path))
        self.table.setItem(r, 0, name)
        if job.kind == "media" and len(job.streams) > 1:
            cb = QComboBox()
            cb.addItems(job.streams)
            cb.currentIndexChanged.connect(lambda i, j=job: setattr(j, "stream", i))
            self.table.setCellWidget(r, 1, cb)
        else:
            self.table.setItem(r, 1, QTableWidgetItem("字幕檔" if job.kind == "srt" else "—"))
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(True)
        self.table.setCellWidget(r, 3, bar)
        self._refresh_row(r)

    def _refresh_row(self, r: int) -> None:
        job = self.jobs[r]
        text = STATE_TEXT.get(job.state, job.state)
        if job.message:
            text = job.message if job.message.startswith(text) else f"{text}:{job.message}"
        it = QTableWidgetItem(text)
        it.setToolTip(text)
        if job.state in STATE_COLOR:
            it.setForeground(QColor(STATE_COLOR[job.state]))
        self.table.setItem(r, 2, it)
        bar = self.table.cellWidget(r, 3)
        if isinstance(bar, QProgressBar):
            bar.setValue(int(job.progress * 1000))
            bar.setFormat(f"{job.progress:.0%}")

    def remove_selected(self) -> None:
        if self.proc is not None:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
            del self.jobs[r]
        self.update_buttons()

    def clear_done(self) -> None:
        if self.proc is not None:
            return
        for r in range(len(self.jobs) - 1, -1, -1):
            if self.jobs[r].state in ("done", "skipped", "no_speech"):
                self.table.removeRow(r)
                del self.jobs[r]
        self.update_buttons()

    # ================================================================== 執行
    def start(self, mode: str = "auto", rows: list[int] | None = None) -> None:
        """mode:auto = 只做還沒做的事(沒字幕就轉錄,有選翻譯目標且還沒有譯文就翻譯)
        translate = 只翻譯;retranscribe = 重新轉錄並覆蓋;retranslate = 重新翻譯並覆蓋。
        rows:只處理這些列(右鍵選單);None = 整個清單。"""
        if self.proc is not None:
            return
        self.save_options()
        s = dict(self.settings)
        tgt = s.get("translate_to")
        if mode in ("translate", "retranslate") and not tgt:
            QMessageBox.information(self, APP_TITLE, "請先在右邊選擇「翻譯目標」。")
            return

        rows = list(range(len(self.jobs))) if rows is None else rows
        jobs, reasons = [], []
        for i in rows:
            plan, why = self._plan(self.jobs[i], mode, s)
            if plan is None:
                if why:
                    reasons.append(f"{self.jobs[i].path.name}:{why}")
                continue
            plan.update(idx=i, stream=self.jobs[i].stream)
            jobs.append(plan)
        if not jobs:
            if not self.jobs:
                msg = "沒有需要處理的檔案。請先加入影片、音檔或字幕檔。"
            elif mode != "auto":
                msg = "選取的檔案沒有需要處理的項目。" + ("\n\n" + "\n".join(reasons[:8]) if reasons else "")
            else:
                msg = ("沒有需要處理的檔案,清單中的工作都已經完成。\n\n"
                       "・要翻譯已轉錄的字幕:先在右邊選擇「翻譯目標」,再按「開始處理」(不會重新轉錄)。\n"
                       "・要重做:在檔案上按右鍵,選「重新轉錄」或「重新翻譯」。")
                if reasons:
                    msg += "\n\n" + "\n".join(reasons[:8])
            QMessageBox.information(self, APP_TITLE, msg)
            return

        if tgt and s.get("translation_engine") in ("anthropic", "openai"):
            from app.translate import keys

            if not keys.has_key(s["translation_engine"]):
                QMessageBox.warning(self, APP_TITLE, "選擇了 API 翻譯,但還沒設定金鑰。請先按「設定(API 金鑰)」。")
                return
        for r in reasons:
            self.append_log(f"略過 {r}")
        for p in jobs:
            j = self.jobs[p["idx"]]
            if p["kind"] == "media" and p.get("overwrite"):
                j.outputs = {}
            j.state, j.progress, j.message = "queued", 0.0, ""
            self._refresh_row(p["idx"])

        from app.jobs import worker

        ctx = mp.get_context("spawn")
        self.q = ctx.Queue()
        self.proc = ctx.Process(target=worker.run, args=(jobs, s, self.q), daemon=True)
        self.proc.start()
        self.started_at = time.monotonic()
        self.got_done = False
        n_tr = sum(p["kind"] == "media" for p in jobs)
        n_tl = len(jobs) - n_tr
        parts = ([f"轉錄 {n_tr} 個"] if n_tr else []) + ([f"只翻譯 {n_tl} 個"] if n_tl else [])
        self.append_log("開始處理:" + "、".join(parts) + (f"(翻譯成 {tgt})" if tgt else ""))
        self.poll_timer.start(150)
        self.update_buttons()

    def _plan(self, job: Job, mode: str, s: dict) -> tuple[dict | None, str]:
        """決定這個檔案這次要做什麼。回傳(工作內容, 不處理的原因)。"""
        from app.translate.runner import planned_output

        tgt = s.get("translate_to")
        bilingual = s.get("subtitle_mode") == "bilingual"
        if job.kind == "media":
            if job.state == "failed" and job.message.startswith("無法讀取"):
                return None, job.message
            if mode == "retranscribe":
                return {"kind": "media", "path": str(job.path), "overwrite": True}, ""
            srt = job.outputs.get("srt")
            if not srt or not Path(srt).is_file():
                if mode == "auto" and job.state != "no_speech":
                    return {"kind": "media", "path": str(job.path)}, ""
                return None, "沒有人聲" if job.state == "no_speech" else "還沒有轉錄,請先按「開始處理」"
            src_file = srt
        else:
            if mode == "retranscribe":
                return None, "字幕檔不能轉錄"
            src_file = str(job.path)
        if not tgt:
            return None, ""
        try:
            dst, src = planned_output(src_file, tgt, bilingual)
        except Exception as e:  # noqa: BLE001
            return None, f"無法讀取字幕:{e}"
        if dst is None:
            return None, f"原文已是 {tgt},不需翻譯"
        if dst.exists() and mode != "retranslate":
            job.outputs["translated"] = str(dst)
            return None, f"已有譯文 {dst.name}(要重做請按右鍵「重新翻譯」)" if mode != "auto" else ""
        return {"kind": "srt", "path": src_file, "overwrite": mode == "retranslate"}, ""

    def show_row_menu(self, pos) -> None:
        if self.proc is not None:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        tgt_label = self.cb_tgt.currentText() if self.cb_tgt.currentData() else None
        tl = f"({tgt_label})" if tgt_label else "(請先選擇翻譯目標)"
        has_media = any(self.jobs[r].kind == "media" for r in rows)
        m = QMenu(self)
        a_tl = m.addAction("只翻譯" + tl)
        a_rtl = m.addAction("重新翻譯,覆蓋舊譯文" + tl)
        a_rtr = m.addAction("重新轉錄,覆蓋舊字幕")
        for a in (a_tl, a_rtl):
            a.setEnabled(bool(tgt_label))
        a_rtr.setEnabled(has_media)
        m.addSeparator()
        a_pv = m.addAction("預覽字幕")
        a_fd = m.addAction("開啟資料夾")
        a_rm = m.addAction("從清單移除")
        act = m.exec(self.table.viewport().mapToGlobal(pos))
        if act is a_tl:
            self.start("translate", rows)
        elif act is a_rtl:
            self.start("retranslate", rows)
        elif act is a_rtr:
            if QMessageBox.question(self, APP_TITLE, "重新轉錄會覆蓋原本的字幕檔"
                                    + ("與譯文" if tgt_label else "") + ",確定嗎?") \
                    == QMessageBox.StandardButton.Yes:
                self.start("retranscribe", rows)
        elif act is a_pv:
            self.preview()
        elif act is a_fd:
            self.open_folder()
        elif act is a_rm:
            self.remove_selected()

    def poll(self) -> None:
        if self.q is None:
            return
        changed = set()
        try:
            for _ in range(500):
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.append_log(msg[1])
                elif kind == "file":
                    _, idx, state, prog, text = msg
                    if 0 <= idx < len(self.jobs):
                        j = self.jobs[idx]
                        j.state = state
                        if prog is not None:
                            j.progress = prog
                        j.message = text
                        changed.add(idx)
                elif kind == "outputs":
                    _, idx, outs = msg
                    if 0 <= idx < len(self.jobs):
                        self.jobs[idx].outputs.update({k: v for k, v in outs.items() if v})
                elif kind == "done":
                    self.got_done = True
        except queue_mod.Empty:
            pass
        except (EOFError, OSError):
            pass
        for idx in changed:
            self._refresh_row(idx)
        elapsed = int(time.monotonic() - self.started_at)
        self.status.setText(f"處理中… {elapsed // 60}:{elapsed % 60:02d}")
        if self.got_done or (self.proc is not None and not self.proc.is_alive()):
            self._finish(crashed=not self.got_done)

    def _finish(self, crashed: bool = False) -> None:
        self.poll_timer.stop()
        if self.proc is not None:
            self.proc.join(3)
        self.proc, self.q = None, None
        for r, j in enumerate(self.jobs):
            if j.state in ("running", "transcribed", "queued") and crashed:
                j.state, j.message = "failed", "工作程序意外結束(詳見 logs\\worker.log)"
                self._refresh_row(r)
        done = sum(j.state == "done" for j in self.jobs)
        failed = sum(j.state == "failed" for j in self.jobs)
        elapsed = int(time.monotonic() - self.started_at)
        self.status.setText(f"完成 {done} 個、失敗 {failed} 個,耗時 {elapsed // 60}:{elapsed % 60:02d}")
        self.append_log("處理結束。" + (" 工作程序意外結束,請查看 logs\\worker.log。" if crashed else ""))
        if not crashed and not self.settings.get("translate_to") and any(
                j.kind == "media" and j.state == "done" for j in self.jobs):
            self.append_log("要翻譯這些字幕:在右邊選擇「翻譯目標」後再按「開始處理」,只會翻譯、不會重新轉錄。")
        self.update_buttons()
        self.refresh_gpu()
        QApplication_alert(self)

    def cancel(self) -> None:
        if self.proc is None:
            return
        if QMessageBox.question(self, APP_TITLE, "確定要取消目前的處理嗎?") != QMessageBox.StandardButton.Yes:
            return
        self.poll_timer.stop()
        self.proc.terminate()
        self.proc.join(5)
        self.proc, self.q = None, None
        for r, j in enumerate(self.jobs):
            if j.state in ("running", "transcribed", "queued"):
                j.state, j.message = "canceled", ""
                self._refresh_row(r)
        self.append_log("已取消,工作程序已結束,顯存已釋放。")
        self.status.setText("已取消")
        self.update_buttons()

    def update_buttons(self) -> None:
        busy = self.proc is not None
        self.btn_start.setEnabled(not busy and bool(self.jobs))
        self.btn_cancel.setEnabled(busy)
        self.btn_add.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_clear.setEnabled(not busy)
        self.btn_settings.setEnabled(not busy)
        self.btn_env.setEnabled(not busy)
        self.table.setAcceptDrops(not busy)
        for w in (self.cb_model, self.cb_src, self.cb_tgt, self.cb_mode, self.cb_engine,
                  self.chk_opencc, self.chk_vad, self.chk_del, self.chk_txt):
            w.setEnabled(not busy)
        if not busy:
            self._toggle_translate_opts()

    # ================================================================== 其他按鈕
    def _current_job(self) -> Job | None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if rows:
            return self.jobs[rows[0]]
        return self.jobs[-1] if self.jobs else None

    def preview(self) -> None:
        from app.gui.dialogs import PreviewDialog

        job = self._current_job()
        if job is None:
            return
        files = [v for k, v in job.outputs.items() if k in ("translated", "srt") and v]
        if job.kind == "srt":
            files.append(str(job.path))
        files = [f for f in dict.fromkeys(files) if Path(f).is_file()]
        if not files:
            QMessageBox.information(self, APP_TITLE, "這個檔案還沒有產生字幕。")
            return
        PreviewDialog(files, self).exec()

    def open_folder(self) -> None:
        job = self._current_job()
        if job is None:
            os.startfile(str(ROOT)) if sys.platform == "win32" else None
            return
        target = job.outputs.get("translated") or job.outputs.get("srt") or str(job.path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(Path(target))])

    def env_check(self) -> None:
        from app.gui.dialogs import EnvCheckDialog

        EnvCheckDialog(self).exec()

    def edit_glossary(self) -> None:
        from app.gui.dialogs import GlossaryDialog

        GlossaryDialog(self).exec()

    def open_settings(self) -> None:
        from app.gui.dialogs import SettingsDialog

        if SettingsDialog(self.settings, self).exec():
            self.append_log("設定已儲存。")
            self.refresh_gpu()

    def append_log(self, text: str) -> None:
        self.log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {text}")

    def closeEvent(self, e):  # noqa: N802
        if self.proc is not None:
            if QMessageBox.question(self, APP_TITLE, "還在處理中,確定要關閉嗎?") != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.proc.terminate()
            self.proc.join(5)
        super().closeEvent(e)


def QApplication_alert(w) -> None:  # noqa: N802
    """處理完成時讓工作列圖示閃爍提醒。"""
    from PySide6.QtWidgets import QApplication

    QApplication.alert(w, 0)
