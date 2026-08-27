import sys
import os
import re
import shutil
import subprocess
import chardet
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QFileDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar,
    QTextEdit, QMessageBox, QGroupBox, QComboBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent


def sanitize_filename(filename: str) -> str:
    """ファイル名に使用できない特殊文字を置換・除去します"""
    return re.sub(r'[\\/*?:"<>|]', '_', filename)

def parse_cue_time(time_str: str) -> float:
    """CUEの MM:SS:FF 形式文字列を秒数(float)に変換します (1 sec = 75 frames)"""
    parts = time_str.strip().split(':')
    if len(parts) != 3:
        return 0.0
    minutes = int(parts[0])
    seconds = int(parts[1])
    frames = int(parts[2])
    return minutes * 60.0 + seconds + (frames / 75.0)

def format_time_display(seconds: float) -> str:
    """秒数を MM:SS.ms 形式にフォーマットします"""
    if seconds is None:
        return "--:--"
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:05.2f}"


class CueTrackInfo:
    def __init__(self, number: int):
        self.number = number
        self.title = ""
        self.performer = ""
        self.start_time = 0.0  # seconds
        self.end_time = None   # seconds (None = end of file)


class CueSheetInfo:
    def __init__(self):
        self.album_title = ""
        self.album_artist = ""
        self.audio_filename = ""
        self.tracks = []


def parse_cue_file(file_path: str) -> CueSheetInfo:
    """CUEファイルをパースしてメタデータとトラックリストを取得します"""
    # 文字コードの自動判別
    with open(file_path, 'rb') as f:
        raw_data = f.read()
        detected = chardet.detect(raw_data)
        encoding = detected['encoding'] or 'utf-8'

    try:
        content = raw_data.decode(encoding)
    except Exception:
        content = raw_data.decode('utf-8', errors='ignore')

    cue_info = CueSheetInfo()
    current_track = None

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        # アルバムレベルの PERFORMER
        if line.startswith('PERFORMER') and current_track is None:
            match = re.search(r'PERFORMER\s+"?([^"]+)"?', line, re.IGNORECASE)
            if match:
                cue_info.album_artist = match.group(1)

        # アルバムレベルの TITLE
        elif line.startswith('TITLE') and current_track is None:
            match = re.search(r'TITLE\s+"?([^"]+)"?', line, re.IGNORECASE)
            if match:
                cue_info.album_title = match.group(1)

        # 音声ファイル名
        elif line.startswith('FILE'):
            match = re.search(r'FILE\s+"?([^"]+)"?', line, re.IGNORECASE)
            if match:
                cue_info.audio_filename = match.group(1)

        # トラック宣言
        elif line.startswith('TRACK'):
            match = re.search(r'TRACK\s+(\d+)', line, re.IGNORECASE)
            if match:
                track_num = int(match.group(1))
                current_track = CueTrackInfo(track_num)
                # デフォルト値の設定
                current_track.performer = cue_info.album_artist
                cue_info.tracks.append(current_track)

        # トラックレベルの TITLE
        elif line.startswith('TITLE') and current_track:
            match = re.search(r'TITLE\s+"?([^"]+)"?', line, re.IGNORECASE)
            if match:
                current_track.title = match.group(1)

        # トラックレベルの PERFORMER
        elif line.startswith('PERFORMER') and current_track:
            match = re.search(r'PERFORMER\s+"?([^"]+)"?', line, re.IGNORECASE)
            if match:
                current_track.performer = match.group(1)

        # トラック開始インデックス (INDEX 01)
        elif line.startswith('INDEX 01') and current_track:
            match = re.search(r'INDEX 01\s+(\d{2}:\d{2}:\d{2})', line, re.IGNORECASE)
            if match:
                current_track.start_time = parse_cue_time(match.group(1))

    # トラックごとの終了時間を計算（次のトラックの開始時間）
    for i in range(len(cue_info.tracks) - 1):
        cue_info.tracks[i].end_time = cue_info.tracks[i + 1].start_time

    return cue_info


class AudioSplitterWorker(QThread):
    progress_changed = pyqtSignal(int, int)  # (current, total)
    log_emitted = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, cue_info: CueSheetInfo, audio_path: str, output_dir: str, reencode: bool):
        super().__init__()
        self.cue_info = cue_info
        self.audio_path = audio_path
        self.output_dir = output_dir
        self.reencode = reencode

    def run(self):
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            self.finished_signal.emit(False, "エラー: システムに ffmpeg が見つかりません。PATHが設定されているか確認してください。")
            return

        os.makedirs(self.output_dir, exist_ok=True)
        _, ext = os.path.splitext(self.audio_path)
        total_tracks = len(self.cue_info.tracks)

        self.log_emitted.emit(f"--- 分割処理を開始します (全 {total_tracks} トラック) ---")
        self.log_emitted.emit(f"入力音源: {self.audio_path}")
        self.log_emitted.emit(f"保存先 directory: {self.output_dir}\n")

        for idx, track in enumerate(self.cue_info.tracks, start=1):
            title_clean = sanitize_filename(track.title or f"Track {track.number:02d}")
            out_filename = f"{track.number:02d}. {title_clean}{ext}"
            out_path = os.path.join(self.output_dir, out_filename)

            # ffmpeg コマンド構築
            cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]

            # 時間指定 (シークオプション)
            cmd.extend(["-ss", f"{track.start_time:.3f}"])
            if track.end_time is not None:
                cmd.extend(["-to", f"{track.end_time:.3f}"])

            cmd.extend(["-i", self.audio_path])

            # コーデック設定 (コピー無劣化か再エンコードか)
            if not self.reencode:
                cmd.extend(["-c", "copy"])

            # メタデータ付与
            cmd.extend(["-metadata", f"title={track.title}"])
            cmd.extend(["-metadata", f"artist={track.performer}"])
            cmd.extend(["-metadata", f"album={self.cue_info.album_title}"])
            cmd.extend(["-metadata", f"track={track.number}/{total_tracks}"])

            cmd.append(out_path)

            self.log_emitted.emit(f"[{idx}/{total_tracks}] トラックを切り出し中: {out_filename}")

            try:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    self.log_emitted.emit(f"  [Error] 分割失敗: {result.stderr}")
            except Exception as e:
                self.log_emitted.emit(f"  [Exception] {str(e)}")

            self.progress_changed.emit(idx, total_tracks)

        self.finished_signal.emit(True, "すべてのトラックの分割が完了しました！")


class CueSplitterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cue_info = None
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("CUE Splitter GUI")
        self.resize(750, 700)
        self.setAcceptDrops(True)

        # メインウィジェット
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(12)

        # --- 入力ファイル選択エリア ---
        file_group = QGroupBox("1. 入力ファイル設定 (ドラッグ＆ドロップ対応)")
        file_layout = QVBoxLayout(file_group)

        # CUE ファイル行
        cue_box = QHBoxLayout()
        cue_box.addWidget(QLabel("CUE ファイル:"))
        self.cue_path_edit = QLineEdit()
        self.cue_path_edit.setPlaceholderText("CUEファイルのパスを選択またはドロップ...")
        cue_box.addWidget(self.cue_path_edit)
        self.btn_browse_cue = QPushButton("参照...")
        self.btn_browse_cue.clicked.connect(self.browse_cue)
        cue_box.addWidget(self.btn_browse_cue)
        file_layout.addLayout(cue_box)

        # 音声 ファイル行
        audio_box = QHBoxLayout()
        audio_box.addWidget(QLabel("音源ファイル:"))
        self.audio_path_edit = QLineEdit()
        self.audio_path_edit.setPlaceholderText("分割する音源ファイル (.flac, .wav, .mp3 など)...")
        audio_box.addWidget(self.audio_path_edit)
        self.btn_browse_audio = QPushButton("参照...")
        self.btn_browse_audio.clicked.connect(self.browse_audio)
        audio_box.addWidget(self.btn_browse_audio)
        file_layout.addLayout(audio_box)

        main_layout.addWidget(file_group)

        # --- 出力先設定エリア ---
        output_group = QGroupBox("2. 保存先ディレクトリ設定")
        output_layout = QVBoxLayout(output_group)

        dir_box = QHBoxLayout()
        self.chk_custom_output = QCheckBox("カスタム出力先を指定する")
        self.chk_custom_output.setChecked(False)
        self.chk_custom_output.toggled.connect(self.toggle_output_dir)
        dir_box.addWidget(self.chk_custom_output)

        self.output_path_edit = QLineEdit()
        self.output_path_edit.setEnabled(False)
        self.output_path_edit.setPlaceholderText("OFFの場合、CUEファイルと同じフォルダに保存されます")
        dir_box.addWidget(self.output_path_edit)

        self.btn_browse_output = QPushButton("参照...")
        self.btn_browse_output.setEnabled(False)
        self.btn_browse_output.clicked.connect(self.browse_output_dir)
        dir_box.addWidget(self.btn_browse_output)

        output_layout.addLayout(dir_box)
        main_layout.addWidget(output_group)

        # --- トラックプレビューテーブル ---
        preview_group = QGroupBox("3. トラック一覧")
        preview_layout = QVBoxLayout(preview_group)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["No.", "タイトル", "アーティスト", "開始時間", "終了時間"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        preview_layout.addWidget(self.table)

        main_layout.addWidget(preview_group)

        # --- 分割実行 & プログレスエリア ---
        control_layout = QHBoxLayout()
        self.btn_start = QPushButton("音声ファイルの分割を開始")
        self.btn_start.setFixedHeight(40)
        self.btn_start.setStyleSheet("font-weight: bold; font-size: 14px; background-color: #2b5c8f; color: white;")
        self.btn_start.clicked.connect(self.start_splitting)
        control_layout.addWidget(self.btn_start)
        main_layout.addLayout(control_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(120)
        main_layout.addWidget(self.log_text)

        # Apply CSS style for clean look
        self.apply_styles()

    def apply_styles(self):
        """UIコンポーネントのデザインスタイルを調整します"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #22252a;
            }
            QWidget {
                color: #e0e0e0;
                font-family: 'Segoe UI', Meiryo, sans-serif;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #3a3f47;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 12px;
                background-color: #2a2e35;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #64b5f6;
            }
            QLineEdit, QTextEdit {
                background-color: #1a1c20;
                border: 1px solid #3a3f47;
                border-radius: 4px;
                padding: 6px;
                color: #ffffff;
            }
            QPushButton {
                background-color: #3d4450;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                color: #ffffff;
            }
            QPushButton:hover {
                background-color: #4f5868;
            }
            QPushButton:disabled {
                background-color: #2c3038;
                color: #666666;
            }
            QTableWidget {
                background-color: #1a1c20;
                gridline-color: #2e333d;
                border: 1px solid #3a3f47;
                border-radius: 4px;
            }
            QHeaderView::section {
                background-color: #2a2e35;
                color: #bbbbbb;
                padding: 4px;
                border: 1px solid #3a3f47;
            }
            QProgressBar {
                border: 1px solid #3a3f47;
                border-radius: 4px;
                text-align: center;
                background-color: #1a1c20;
            }
            QProgressBar::chunk {
                background-color: #42a5f5;
                border-radius: 3px;
            }
        """)

    def browse_cue(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "CUEファイルを選択", "", "CUE Files (*.cue);;All Files (*)"
        )
        if file_path:
            self.cue_path_edit.setText(file_path)
            self.load_cue(file_path)

    def browse_audio(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "音源ファイルを選択", "", "Audio Files (*.flac *.wav *.mp3 *.ape *.m4a *.aac);;All Files (*)"
        )
        if file_path:
            self.audio_path_edit.setText(file_path)

    def browse_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "出力先ディレクトリを選択")
        if dir_path:
            self.output_path_edit.setText(dir_path)

    def toggle_output_dir(self, checked: bool):
        self.output_path_edit.setEnabled(checked)
        self.btn_browse_output.setEnabled(checked)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if not urls:
            return

        for url in urls:
            file_path = url.toLocalFile()
            ext = os.path.splitext(file_path)[1].lower()

            if ext == '.cue':
                self.cue_path_edit.setText(file_path)
                self.load_cue(file_path)
            elif ext in ['.flac', '.wav', '.mp3', '.ape', '.m4a', '.aac', '.ogg']:
                self.audio_path_edit.setText(file_path)

    def load_cue(self, cue_path: str):
        try:
            self.cue_info = parse_cue_file(cue_path)
            self.update_table()

            # 音源ファイルの推測
            cue_dir = os.path.dirname(cue_path)
            guessed_audio = ""

            # 1. CUE内のFILE指定からの読み込み
            if self.cue_info.audio_filename:
                possible_path = os.path.join(cue_dir, self.cue_info.audio_filename)
                if os.path.exists(possible_path):
                    guessed_audio = possible_path

            # 2. CUEと同じ名前で拡張子が異なるファイルを検索
            if not guessed_audio:
                base_name = os.path.splitext(cue_path)[0]
                for ext in ['.flac', '.wav', '.mp3', '.ape', '.m4a']:
                    if os.path.exists(base_name + ext):
                        guessed_audio = base_name + ext
                        break

            if guessed_audio:
                self.audio_path_edit.setText(guessed_audio)

            self.log_text.append(f"CUEファイルを読み込みました: {os.path.basename(cue_path)}")
            self.log_text.append(f"アルバム: {self.cue_info.album_title} / アーティスト: {self.cue_info.album_artist}")

        except Exception as e:
            QMessageBox.critical(self, "エラー", f"CUEファイルの解析に失敗しました:\n{str(e)}")

    def update_table(self):
        if not self.cue_info:
            return

        self.table.setRowCount(0)
        for row, track in enumerate(self.cue_info.tracks):
            self.table.insertRow(row)

            item_num = QTableWidgetItem(f"{track.number:02d}")
            item_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, item_num)

            self.table.setItem(row, 1, QTableWidgetItem(track.title))
            self.table.setItem(row, 2, QTableWidgetItem(track.performer))

            start_str = format_time_display(track.start_time)
            end_str = format_time_display(track.end_time) if track.end_time else "End"

            item_start = QTableWidgetItem(start_str)
            item_start.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 3, item_start)

            item_end = QTableWidgetItem(end_str)
            item_end.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 4, item_end)

    def start_splitting(self):
        cue_path = self.cue_path_edit.text().strip()
        audio_path = self.audio_path_edit.text().strip()

        if not cue_path or not os.path.exists(cue_path):
            QMessageBox.warning(self, "警告", "有効なCUEファイルを選択してください。")
            return

        if not audio_path or not os.path.exists(audio_path):
            QMessageBox.warning(self, "警告", "有効な音源ファイルを選択してください。")
            return

        if not self.cue_info or not self.cue_info.tracks:
            QMessageBox.warning(self, "警告", "分割対象のトラック情報がありません。")
            return

        # 保存先ディレクトリの設定
        if self.chk_custom_output.isChecked():
            output_dir = self.output_path_edit.text().strip()
            if not output_dir:
                QMessageBox.warning(self, "警告", "出力先ディレクトリを指定してください。")
                return
        else:
            output_dir = os.path.dirname(cue_path)

        # UI操作の無効化
        self.btn_start.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_text.clear()

        # スレッド起動
        self.worker = AudioSplitterWorker(
            cue_info=self.cue_info,
            audio_path=audio_path,
            output_dir=output_dir,
            reencode=False
        )
        self.worker.progress_changed.connect(self.on_progress)
        self.worker.log_emitted.connect(self.on_log)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def on_progress(self, current: int, total: int):
        percent = int((current / total) * 100)
        self.progress_bar.setValue(percent)

    def on_log(self, message: str):
        self.log_text.append(message)

    def on_finished(self, success: bool, message: str):
        self.btn_start.setEnabled(True)
        if success:
            QMessageBox.information(self, "完了", message)
        else:
            QMessageBox.critical(self, "エラー", message)


def main():
    app = QApplication(sys.argv)
    window = CueSplitterApp()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()