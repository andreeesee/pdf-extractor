import os
import re
import time
import logging
import sys
import traceback
import base64
from io import BytesIO

from flask import Flask, request, jsonify, send_file
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.errors import PdfReadWarning
from pdf2image import convert_from_bytes
import pytesseract
from PIL import Image, ImageFilter

import warnings
warnings.filterwarnings("ignore", category=PdfReadWarning)

pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

# Config
API_KEY = os.getenv("API_KEY")
POPPLER_PATH = os.getenv("POPPLER_PATH", "/usr/bin")

app = Flask(__name__)

# Logging Setup
if "gunicorn" in sys.modules:
    gunicorn_logger = logging.getLogger("gunicorn.error")
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    app.logger.handlers = []
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

def preprocess_image_for_ocr(img: Image.Image) -> Image.Image:
    img = img.convert("L")
    img = img.point(lambda x: 0 if x < 140 else 255, "1")
    img = img.filter(ImageFilter.MedianFilter(size=3))
    return img

def extract_text_with_ocr(pdf_bytes):
    reader = PdfReader(BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    app.logger.info(f"OCR total pages: {total_pages}")

    full_text = []
    for i in range(total_pages):
        writer = PdfWriter()
        writer.add_page(reader.pages[i])
        buf = BytesIO()
        writer.write(buf)
        buf.seek(0)

        images = convert_from_bytes(buf.read(), dpi=300, poppler_path=POPPLER_PATH)
        img = preprocess_image_for_ocr(images[0])
        text = pytesseract.image_to_string(img, config="--psm 3 --oem 3")

        app.logger.info(f"OCR page {i+1}/{total_pages}: {len(text.strip())} chars")
        full_text.append(text)

    return "\n".join(full_text)

@app.route("/health", methods=["GET"])
def health_check():
    key = request.headers.get("X-API-Key")
    if key and key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"status": "ok"}), 200

@app.route("/", methods=["POST"])
def process_pdf():
    start_time = time.time()
    try:
        data = request.get_json(force=True, silent=True)
        app.logger.info("Start Request")

        if request.headers.get("X-API-Key") != API_KEY:
            app.logger.warning("Unauthorized request")
            return jsonify({"error": "Unauthorized"}), 401

        if not data or "file" not in data:
            return jsonify({"error": "Invalid input, missing 'file'"}), 400

        action = data.get("action", "").lower()
        pdf_data = base64.b64decode(data["file"])
        password = data.get("password", "")
        filename = data.get("filename", "output.pdf")

        if action == "extract":
            app.logger.info("Processing extract action (Raw Text Only)")
            reader = PdfReader(BytesIO(pdf_data))

            if reader.is_encrypted:
                if password:
                    if reader.decrypt(password) == 0:
                        return jsonify({"error": "Incorrect password"}), 400
                else:
                    return jsonify({"error": "Password required for encrypted PDF"}), 400

            # 1. Attempt Standard Extraction
            page_texts = []
            for idx, page in enumerate(reader.pages, start=1):
                try:
                    t = page.extract_text() or ""
                    page_texts.append(t)
                except Exception as e:
                    app.logger.warning(f"PyPDF2 extraction failed on page {idx}: {e}")

            full_text = "".join(page_texts).strip()
            used_ocr = False

            # 2. Force OCR if standard extraction is empty or requested
            # Keeping your existing logic that forces OCR for high accuracy
            app.logger.info("Running OCR for full text capture")
            full_text = extract_text_with_ocr(pdf_data)
            used_ocr = True

            return jsonify({
                "raw_text_preview": full_text,
                "meta": {
                    "used_ocr": used_ocr,
                    "pages": len(reader.pages)
                }
            })

        elif action == "decrypt":
            app.logger.info("Processing decrypt action")
            reader = PdfReader(BytesIO(pdf_data))

            if reader.is_encrypted:
                if password:
                    if reader.decrypt(password) == 0:
                        return jsonify({"error": "Incorrect password"}), 400
                else:
                    return jsonify({"error": "Password required"}), 400

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            output = BytesIO()
            writer.write(output)
            output.seek(0)

            return send_file(
                output,
                as_attachment=True,
                download_name=f"decrypted_{filename}",
                mimetype="application/pdf"
            )

        else:
            return jsonify({"error": f"Unknown action '{action}'"}), 400

    except Exception as e:
        app.logger.error(f"Exception: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

    finally:
        elapsed = time.time() - start_time
        app.logger.info(f"Request processed in {elapsed:.2f} seconds")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)), debug=True)