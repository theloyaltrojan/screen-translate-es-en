# Screen Translate (Spanish → English)

A small always-on-top macOS GUI that captures a region of your screen, OCRs the Spanish text with Tesseract, and shows the English translation. Includes a "Live" mode that watches a chosen region and re-translates whenever the text changes.

## Features
- Select a region and translate it once
- Capture the full screen and translate it
- Live mode: auto-retranslate when the on-screen text changes

## Install

```
brew install tesseract tesseract-lang python-tk@3.14
pip3 install pytesseract deep-translator pillow
```

## Run

```
python3 screen_translate_gui.py
```
