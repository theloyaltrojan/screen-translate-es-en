# Screen Translate

A small always-on-top macOS GUI that captures a region of your screen, OCRs the text with Tesseract, and shows the translation. Includes a "Live" mode that watches a chosen region and re-translates whenever the text changes.

Pick any source/target language pair from the header dropdowns — defaults to Spanish → English. Supported: Spanish, English, French, German, Portuguese, Italian, Dutch, Russian, Chinese (Simplified), Japanese, Korean, Arabic.

## Features
- Select a region and translate it once
- Capture the full screen and translate it
- Live mode: auto-retranslate when the on-screen text changes
- Configurable source/target language pair
- Optional "source language only" filter to ignore other on-screen text

## Install

```
brew install tesseract tesseract-lang python-tk@3.14
pip3 install pytesseract deep-translator pillow
```

## Run

```
python3 screen_translate_gui.py
```
