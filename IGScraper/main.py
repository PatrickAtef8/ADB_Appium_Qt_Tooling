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

    # ====================== SET APPLICATION ICON ======================
    icon_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 
        "Cansav2.png"
    )
    
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        print(f"✅ Application icon loaded: {icon_path}")
    else:
        print(f"⚠️  WARNING: Cansa.png not found at:\n   {icon_path}")
    # ================================================================

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()