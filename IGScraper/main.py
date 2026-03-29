"""
Instagram Follower Scraper
Entry point — launches the PyQt6 GUI.
"""
import sys
import os

# Ensure project root is on the path (important for PyInstaller)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from src.ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Instagram Scraper")
    app.setApplicationVersion("1.0")

    # High-DPI support is automatic in PyQt6
    # app.setStyle("Fusion") # QFluentWidgets handles styling

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
