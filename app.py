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

import sys
import logging

if 'gunicorn' in sys.modules:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO) #DEBUG
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    app.logger.handlers = []
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO) #DEBUG

# If running under Gunicorn, use its logger handlers and level
if __name__ != "__main__":
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

def extract_fields(text: str) -> dict:
    import re

    # Normalize whitespace to single spaces for easier regex matching
    text = re.sub(r"\s+", " ", text).strip()

    def safe_search(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m and m.lastindex >= 1 else None

    # Card number patterns (keep all existing ones)
    card_number_patterns = [
        r"(\d{4}-\d{4}-\d{4}-\d{4})",  # RCBC fallback
        r"(\d{6}-\d-\d{2}-\d{7,})\s*-",
        r"\b((?:\d{4}[\s\-]?){3}\d{4})\b",
        r"\b((?:[\d\*Xx]{4}[\s\-]?){3}[\d\*Xx]{4})\b",
        r"CARD NUMBER[:\s\-]*([\d\*Xx\- ]+)",
        r"Card Number[:\s\-]*([\d\*Xx\- ]+)",
        r"Card No\.?[:\s\-]*([\d\*Xx\- ]+)",
        r"HSBC [A-Z ]+ VISA (\d{4}-\d{4}-\d{4}-\d{4})",
        r"VISA\s+([\d\*Xx\-]{4,})",
        r"CARD NUMBER\s*[:\-]?\s*([\d\*Xx\-]{4,})"        
    ]

    card_number = None
    for pattern in card_number_patterns:
        card_number = safe_search(pattern)
        if card_number:
            break

    # Helper to find dates
    def find_date(patterns):
        for p in patterns:
            d = safe_search(p)
            if d:
                return d
        return None

    # Dates � existing patterns
    statement_date = find_date([
        r"STATEMENT DATE\s*[:\-]?\s*([A-Z]{3,9} \d{1,2},? \d{4})",
        r"Statement Date\s*[:\-]?\s*([A-Z][a-z]{2,8} \d{1,2},? \d{4})",
        r"Statement Date[:\s]*([0-9]{1,2} [A-Z][a-z]{2} \d{4})",
        r"Statement From (\d{1,2} [A-Z]{3,9} \d{4}) to",
        r"STATEMENT\s*DATE\s*([A-Z]+\s+\d{1,2},?\d{4})",
    ])
    payment_due_date = find_date([
        r"PAYMENT DUE DATE\s*[:\-]?\s*([A-Z]{3,9} \d{1,2},? \d{4})",
        r"Payment Due Date\s*[:\-]?\s*([0-9]{1,2} [A-Z][a-z]{2} \d{4})",
        r"Payment Due Date (\d{1,2} [A-Z]{3} \d{4})",
        r"PAYMENT\s*DUEDATE\s*([A-Z]+\s+\d{1,2},?\d{4})",
    ])

    # Amounts
    amount_pattern = r"(?:[?$]|PHP)?\s*([\d,]+(?:\.\d{1,2})?)"
    statement_balance = safe_search(
        rf"(?:TOTAL AMOUNT DUE|TOTAL BALANCE DUE|TOTAL DUE|(?<![A-Z])TOTAL(?!S|\w)|(?<!PREVIOUS )STATEMENT BALANCE)\s*[:\-]?\s*{amount_pattern}"
    )
    minimum_amount_due = safe_search(
        rf"(?<!\w)(?:MINIMUM AMOUNT DUE|MINIMUM PAYMENT)(?!\w)\s*[:\-]?\s*{amount_pattern}"
    )

    # RCBC fallback for amounts (pick from inline "P <amount>" section)
    if not statement_balance:
        m = re.search(r"TOTAL BALANCE DUE\s+[A-Z]{3} \d{1,2} \d{4}.*?P\s*([\d,]+\.\d{2})", text)
        if m:
            statement_balance = m.group(1)

    # AirAsia / Generic fallback for amounts before "END OF STATEMENT"
    if not statement_balance or statement_balance == "1":
        m = re.search(r"END OF STATEMENT.*?([\d,]+\.\d{2})", text, re.IGNORECASE | re.DOTALL)
        if m:
            statement_balance = m.group(1)

    if not minimum_amount_due:
        m = re.search(r"MINIMUM\s+PAYMENT\s+DUE\s*P\s*([\d,]+\.\d{2})", text)
        if m:
            minimum_amount_due = m.group(1)

    # Extract Payment Due Date & Statement Date (AirAsia / RCBC layout)
    if not payment_due_date or not statement_date:
        m = re.search(
            r"MINIMUM\s*PAYMENT\s*DUE.*?TOTAL\s*BALANCE\s*DUE\s*([A-Z]{3}\s*\d{1,2}\s*\d{4})\s*([A-Z]{3}\s*\d{1,2}\s*\d{4})",
            text,
            re.IGNORECASE | re.DOTALL
        )
        if m:
            payment_due_date = m.group(1).replace(" ", " ").strip()
            statement_date = m.group(2).replace(" ", " ").strip()

    points_earned = safe_search(
        r"(?:earned\s*|BIG Points\s*)([\d,]+)"
    )

    return {
        "card_number": card_number,
        "statement_date": statement_date,
        "payment_due_date": payment_due_date,
        "statement_balance": statement_balance,
        "minimum_amount_due": minimum_amount_due,
        "points_earned": points_earned
    }



def preprocess_image_for_ocr(img: Image.Image) -> Image.Image:
    img = img.convert("L")  # grayscale
    img = img.point(lambda x: 0 if x < 140 else 255, '1')  # binarize with threshold 140
    img = img.filter(ImageFilter.MedianFilter(size=3))    # median filter to reduce noise
    return img

def extract_text_with_ocr(pdf_bytes):
    if POPPLER_PATH:
        pages = convert_from_bytes(pdf_bytes, dpi=300, poppler_path=POPPLER_PATH)
    else:
        pages = convert_from_bytes(pdf_bytes, dpi=300)

    full_text = []
    for i, page in enumerate(pages, start=1):
        try:
            img = preprocess_image_for_ocr(page)
            text = pytesseract.image_to_string(img, config="--psm 3 --oem 3")
            app.logger.info(f"OCR page {i}: {len(text.strip())} chars extracted")
            full_text.append(text)
        except Exception as e:
            app.logger.warning(f"OCR failed on page {i}: {e}")
            full_text.append("")
    return "\n".join(full_text)

def looks_like_encoded(text: str) -> bool:
    count = len(re.findall(r"/C[0-9A-Fa-f]{2}", text))
    return count > 10

@app.route("/health", methods=["GET"])
def health_check():
    key = request.headers.get("X-API-Key")
    if key and key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"status": "ok"}), 200

@app.route("/logtest")
def log_test():
    app.logger.info("Logtest endpoint was hit")
    return "Log test OK"

@app.route("/", methods=["POST"])
def process_pdf():
    start_time = time.time()
    try:
        data = request.get_json(force=True, silent=True)
        app.logger.info(f"Start")

        if request.headers.get("X-API-Key") != API_KEY:
            app.logger.warning("Unauthorized request")
            return jsonify({"error": "Unauthorized"}), 401

        if not data or "file" not in data:
            return jsonify({"error": "Invalid input, missing 'file'"}), 400

        action = data.get("action")
        if not action:
            return jsonify({"error": "Missing 'action' parameter"}), 400
        action = action.lower()

        pdf_data = base64.b64decode(data["file"])
        password = data.get("password", "")
        filename = data.get("filename", "output.pdf")

        if action == "extract":
            app.logger.info("Processing extract action")
            reader = PdfReader(BytesIO(pdf_data))

            if reader.is_encrypted:
                if password:
                    decrypt_result = reader.decrypt(password)
                    if decrypt_result == 0:
                        return jsonify({"error": "Incorrect password"}), 400
                else:
                    return jsonify({"error": "Password required for encrypted PDF"}), 400

            page_texts = []
            for idx, page in enumerate(reader.pages, start=1):
                try:
                    t = page.extract_text() or ""
                except Exception as e:
                    app.logger.warning(f"PyPDF2 extract_text failed on page {idx}: {e}")
                    t = ""
                app.logger.info(f"Page {idx}: {len(t.strip())} chars extracted")
                page_texts.append(t)

            full_text = "".join(page_texts).strip()
            
            app.logger.info(f"NEW Full extracted text preview (first 1000 chars):\n{full_text[:1000]}")

            garbled_pattern = r"[ÃÂäöÔÅÙ]"
            used_ocr = False
            
            app.logger.info("Forcing OCR on all pages")
            full_text = extract_text_with_ocr(pdf_data)
            used_ocr = True

            # if (not full_text) or re.search(garbled_pattern, full_text) or len(full_text) < 20 or looks_like_encoded(full_text):
            #     app.logger.info("Text extraction failed or garbled or looks encoded; falling back to OCR")
            #     full_text = extract_text_with_ocr(pdf_data)
            #     used_ocr = True
            # else:
            #     app.logger.info("Text extraction succeeded without OCR")

            parsed = extract_fields(full_text)
            preview = full_text[:99999]
            return jsonify({
                "extracted": parsed,
                "meta": {
                    "used_ocr": used_ocr,
                    "raw_text_preview": preview
                }
            })

        elif action == "decrypt":
            app.logger.info("Processing decrypt action")
            reader = PdfReader(BytesIO(pdf_data))

            if reader.is_encrypted:
                if password:
                    decrypt_result = reader.decrypt(password)
                    if decrypt_result == 0:
                        return jsonify({"error": "Incorrect password"}), 400
                else:
                    return jsonify({"error": "Password required for encrypted PDF"}), 400

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            output = BytesIO()
            writer.write(output)
            output.seek(0)

            app.logger.info(f"Returning decrypted PDF: decrypted_{filename}")
            resp = send_file(
                output,
                as_attachment=True,
                download_name=f"decrypted_{filename}",
                mimetype="application/pdf"
            )
            return resp

        else:
            app.logger.warning(f"Unknown action requested: {action}")
            return jsonify({"error": f"Unknown action '{action}'"}), 400

    except Exception as e:
        app.logger.error(f"Exception: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500

    finally:
        elapsed = time.time() - start_time
        app.logger.info(f"Request processed in {elapsed:.2f} seconds")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)), debug=True)
