"""對話框:設定(API 金鑰等)、詞彙表、字幕預覽、環境檢查。"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app import settings as settings_mod
from app.paths import ROOT


# ====================================================================== 設定
class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
        from app.translate import keys

        self.keys = keys
        self.settings = settings
        self.setWindowTitle("設定")
        self.resize(560, 520)
        api = settings.setdefault("api", {})
        seg = settings.setdefault("segmenter", {})
        lay = QVBoxLayout(self)

        # Claude
        g1 = QGroupBox("Claude API")
        f1 = QFormLayout(g1)
        self.anth_key = QLineEdit()
        self.anth_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.anth_key.setPlaceholderText("已儲存金鑰" if keys.has_key("anthropic") else "尚未設定")
        self.anth_model = QLineEdit(api.get("anthropic_model", ""))
        self.anth_model.setPlaceholderText("留空 = 自動選最新的 Sonnet")
        f1.addRow("API 金鑰", self.anth_key)
        f1.addRow("模型名稱", self.anth_model)
        lay.addWidget(g1)

        # OpenAI 相容
        g2 = QGroupBox("OpenAI 相容 API(OpenAI、Azure、公司自架服務)")
        f2 = QFormLayout(g2)
        self.oa_key = QLineEdit()
        self.oa_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.oa_key.setPlaceholderText("已儲存金鑰" if keys.has_key("openai") else "尚未設定")
        self.oa_url = QLineEdit(api.get("openai_base_url", ""))
        self.oa_url.setPlaceholderText("官方 OpenAI 請留空;第三方或公司服務請填網址")
        self.oa_model = QLineEdit(api.get("openai_model", ""))
        f2.addRow("API 金鑰", self.oa_key)
        f2.addRow("服務網址", self.oa_url)
        f2.addRow("模型名稱", self.oa_model)
        lay.addWidget(g2)
        hint = QLabel("金鑰存在 Windows 認證管理員,不會寫進設定檔。欄位留空表示不變更。")
        hint.setStyleSheet("color: gray;")
        lay.addWidget(hint)

        # 處理
        g3 = QGroupBox("處理")
        f3 = QFormLayout(g3)
        self.overwrite = QCheckBox("已有同名字幕檔時直接覆蓋(預設略過)")
        self.overwrite.setChecked(bool(settings.get("overwrite")))
        self.batch = QSpinBox()
        self.batch.setRange(0, 32)
        self.batch.setSpecialValueText("自動(依顯存)")
        self.batch.setValue(int(settings.get("batch_size") or 0))
        self.cjk = QSpinBox()
        self.cjk.setRange(8, 40)
        self.cjk.setValue(int(seg.get("max_chars_cjk", 18)))
        self.latin = QSpinBox()
        self.latin.setRange(20, 80)
        self.latin.setValue(int(seg.get("max_chars_latin", 42)))
        self.merge = QCheckBox("離線翻譯先合併跨條句子(實測 NLLB 逐條較完整,建議不勾)")
        self.merge.setChecked(bool(settings.get("offline_merge")))
        f3.addRow(self.overwrite)
        f3.addRow("轉錄批次大小", self.batch)
        f3.addRow("中日韓每行字數", self.cjk)
        f3.addRow("英文等每行字元", self.latin)
        f3.addRow(self.merge)
        lay.addWidget(g3)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def save(self) -> None:
        s = self.settings
        api = s.setdefault("api", {})
        try:
            if self.anth_key.text().strip():
                self.keys.set_key("anthropic", self.anth_key.text())
            if self.oa_key.text().strip():
                self.keys.set_key("openai", self.oa_key.text())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "無法儲存金鑰", str(e))
            return
        api["anthropic_model"] = self.anth_model.text().strip()
        api["openai_base_url"] = self.oa_url.text().strip()
        api["openai_model"] = self.oa_model.text().strip()
        s["overwrite"] = self.overwrite.isChecked()
        s["batch_size"] = self.batch.value() or None
        seg = s.setdefault("segmenter", {})
        seg["max_chars_cjk"] = self.cjk.value()
        seg["max_chars_latin"] = self.latin.value()
        s["offline_merge"] = self.merge.isChecked()
        settings_mod.save(s)
        self.accept()


# ====================================================================== 詞彙表
class GlossaryDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from app.translate.glossary import GLOSSARY_FILE

        self.path = GLOSSARY_FILE
        self.setWindowTitle("詞彙表")
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("一行一個詞。寫成「原詞=譯詞」時,翻譯會固定使用這個譯法;只寫原詞則只用來提高轉錄辨識率。"
                             "\n# 開頭的行是註解。例:熱軋=hot rolling"))
        self.edit = QPlainTextEdit()
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.edit.setFont(f)
        if self.path.is_file():
            self.edit.setPlainText(self.path.read_text(encoding="utf-8-sig", errors="replace"))
        lay.addWidget(self.edit)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.edit.toPlainText().rstrip() + "\n", encoding="utf-8")
        self.accept()


# ====================================================================== 預覽
class PreviewDialog(QDialog):
    """唯讀預覽字幕;疑似有問題的條目(太短、字數過密、疑似幻覺句)以黃色標示。"""

    def __init__(self, files: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("預覽字幕")
        self.resize(900, 600)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.combo = QComboBox()
        for f in files:
            self.combo.addItem(Path(f).name, f)
        top.addWidget(QLabel("檔案"))
        top.addWidget(self.combo, 1)
        lay.addLayout(top)
        self.info = QLabel("")
        self.info.setStyleSheet("color: gray;")
        lay.addWidget(self.info)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "開始", "結束", "字幕"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        for c in (0, 1, 2):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.combo.currentIndexChanged.connect(self.load)
        self.load()

    def load(self) -> None:
        from app.core import postprocess, srt_io

        path = self.combo.currentData()
        self.table.setRowCount(0)
        if not path or not Path(path).is_file():
            self.info.setText("找不到檔案")
            return
        cues = srt_io.read_srt(path)
        flagged = 0
        for i, c in enumerate(cues):
            r = self.table.rowCount()
            self.table.insertRow(r)
            text = c.text
            dur = max(0.01, c.end - c.start)
            dense = len(text.replace("\n", "")) / dur > 15
            reason = []
            if dur < 0.5:
                reason.append("顯示時間很短")
            if dense:
                reason.append("字數相對時間過多")
            if postprocess.is_hallucination(text.replace("\n", "")):
                reason.append("疑似幻覺句")
            items = [str(i + 1), srt_io.format_ts(c.start), srt_io.format_ts(c.end), text]
            for col, v in enumerate(items):
                it = QTableWidgetItem(v)
                if reason:
                    it.setBackground(QColor("#fff4c2"))
                    it.setForeground(QColor("#000000"))
                    it.setToolTip("、".join(reason))
                self.table.setItem(r, col, it)
            flagged += bool(reason)
        self.table.resizeRowsToContents()
        self.info.setText(f"共 {len(cues)} 條" + (f",{flagged} 條以黃色標示需要留意(滑鼠移上去看原因)" if flagged else ""))


# ====================================================================== 環境檢查
class EnvCheckDialog(QDialog):
    """在獨立程序執行環境檢查(含 GPU 測試),不讓 GUI 本身占用顯存。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("環境檢查")
        self.resize(820, 420)
        lay = QVBoxLayout(self)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        lay.addWidget(self.out, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.out.setPlainText("檢查中(含 GPU 推論測試,約 10~60 秒)…\n")
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(str(ROOT))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(lambda *_: self.out.appendPlainText("\n(檢查結束)"))
        self.proc.start(_python_exe(), ["-m", "app.envcheck", "--smoke"])

    def _read(self) -> None:
        data = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        self.out.insertPlainText(data)
        self.out.ensureCursorVisible()

    def reject(self) -> None:
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)
        super().reject()


def _python_exe() -> str:
    """GUI 可能是用 pythonw.exe 啟動;子程序用 python.exe 才拿得到輸出。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)
