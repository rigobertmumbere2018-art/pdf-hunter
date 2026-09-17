from flask import Flask, render_template, request, jsonify, send_file, after_this_request
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from werkzeug.utils import secure_filename
import requests
import zipfile
import tempfile
import os
import re

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
TYPE_FOLDERS = {
    ".pdf": "PDF",
    ".jpg": "JPG",
    ".jpeg": "JPEG",
    ".png": "PNG"
}
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_FILES = 1000


def is_safe_http_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def get_extension_from_url(url):
    path = urlparse(url).path.lower()
    for ext in ALLOWED_EXTENSIONS:
        if path.endswith(ext):
            return ext
    return ""


def get_extension_from_content_type(content_type):
    content_type = (content_type or "").lower().split(";")[0].strip()
    mapping = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png"
    }
    return mapping.get(content_type, "")


def detect_extension(url, content_type="", first_bytes=b""):
    ext = get_extension_from_url(url)
    if ext:
        return ext

    ext = get_extension_from_content_type(content_type)
    if ext:
        return ext

    if first_bytes.startswith(b"%PDF"):
        return ".pdf"
    if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if first_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"

    return ""


def clean_filename(name, fallback):
    name = (name or "").strip()
    name = name.replace("/", "_").replace("\\", "_")
    name = secure_filename(name)
    if not name:
        name = fallback
    return name[:180]


def unique_archive_name(existing, filename):
    base, ext = os.path.splitext(filename)
    candidate = filename
    number = 2
    while candidate.lower() in existing:
        candidate = f"{base}_{number}{ext}"
        number += 1
    existing.add(candidate.lower())
    return candidate


def download_to_temp(url, referer=""):
    headers = dict(HEADERS)
    if referer and is_safe_http_url(referer):
        headers["Referer"] = referer

    last_error = None

    for _ in range(3):
        temp_path = None
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=(15, 60),
                stream=True,
                allow_redirects=True
            )
            response.raise_for_status()

            final_url = response.url
            content_type = response.headers.get("content-type", "")
            content_length = response.headers.get("content-length")

            if content_length:
                try:
                    if int(content_length) > MAX_FILE_SIZE:
                        raise ValueError("Fichier trop volumineux")
                except ValueError as exc:
                    if str(exc) == "Fichier trop volumineux":
                        raise

            temp = tempfile.NamedTemporaryFile(delete=False, suffix=".download")
            temp_path = temp.name
            total = 0
            first_bytes = b""

            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue

                if not first_bytes:
                    first_bytes = chunk[:32]

                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    raise ValueError("Fichier trop volumineux")

                temp.write(chunk)

            temp.close()
            response.close()

            extension = detect_extension(final_url, content_type, first_bytes)
            if extension not in ALLOWED_EXTENSIONS:
                raise ValueError("Type de fichier non pris en charge")

            return temp_path, extension, final_url

        except Exception as exc:
            last_error = exc
            try:
                response.close()
            except Exception:
                pass
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    raise last_error or RuntimeError("Téléchargement impossible")


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/scan")
def scan():
    data = request.get_json(silent=True) or {}
    page_url = (data.get("url") or "").strip()

    if not is_safe_http_url(page_url):
        return jsonify({
            "error": "Veuillez entrer une URL valide commençant par http:// ou https://"
        }), 400

    try:
        response = requests.get(
            page_url,
            headers=HEADERS,
            timeout=(15, 30),
            allow_redirects=True
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({"error": f"Impossible d'ouvrir cette page : {exc}"}), 400

    content_type = response.headers.get("content-type", "")
    page_ext = detect_extension(response.url, content_type, response.content[:32])

    if page_ext in ALLOWED_EXTENSIONS:
        filename = os.path.basename(urlparse(response.url).path) or f"document{page_ext}"
        return jsonify({
            "files": [{
                "name": clean_filename(filename, f"document{page_ext}"),
                "url": response.url,
                "type": TYPE_FOLDERS[page_ext],
                "extension": page_ext[1:].upper()
            }],
            "count": 1
        })

    soup = BeautifulSoup(response.text, "html.parser")
    found = []
    seen = set()

    tags = soup.find_all(["a", "img", "source", "video", "audio", "iframe", "embed", "object", "link"])
    attributes = ["href", "src", "data-src", "data-href", "data-original", "data-url"]

    for tag in tags:
        for attribute in attributes:
            raw_url = tag.get(attribute)
            if not raw_url:
                continue

            clean_url = urljoin(response.url, raw_url).split("#")[0]
            if not is_safe_http_url(clean_url) or clean_url in seen:
                continue

            extension = get_extension_from_url(clean_url)
            if extension not in ALLOWED_EXTENSIONS:
                continue

            seen.add(clean_url)

            name = tag.get_text(" ", strip=True)
            if not name:
                name = os.path.basename(urlparse(clean_url).path)
            if not name:
                name = f"fichier{extension}"

            if not os.path.splitext(name)[1]:
                name += extension

            found.append({
                "name": clean_filename(name, f"fichier{extension}"),
                "url": clean_url,
                "type": TYPE_FOLDERS[extension],
                "extension": extension[1:].upper()
            })
            break

    found.sort(key=lambda item: (item["extension"], item["name"].lower()))

    return jsonify({
        "files": found,
        "count": len(found)
    })


@app.post("/download-all")
def download_all():
    data = request.get_json(silent=True) or {}
    files = data.get("files") or data.get("pdfs") or []
    page_url = (data.get("page_url") or "").strip()

    if not isinstance(files, list) or not files:
        return jsonify({"error": "Aucun fichier à télécharger."}), 400

    if len(files) > MAX_FILES:
        return jsonify({"error": f"Maximum {MAX_FILES} fichiers par téléchargement."}), 400

    zip_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    zip_path = zip_temp.name
    zip_temp.close()

    downloaded = 0
    failed = 0
    used_names = {
        ".pdf": set(),
        ".jpg": set(),
        ".jpeg": set(),
        ".png": set()
    }

    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, item in enumerate(files, start=1):
                if not isinstance(item, dict):
                    failed += 1
                    continue

                url = (item.get("url") or "").strip()
                if not is_safe_http_url(url):
                    failed += 1
                    continue

                temp_path = None
                try:
                    temp_path, extension, final_url = download_to_temp(url, page_url)

                    requested_name = item.get("name") or os.path.basename(urlparse(final_url).path)
                    fallback = f"fichier_{index}{extension}"
                    filename = clean_filename(requested_name, fallback)

                    current_ext = os.path.splitext(filename)[1].lower()
                    if current_ext not in ALLOWED_EXTENSIONS:
                        filename += extension
                    elif current_ext != extension:
                        filename = os.path.splitext(filename)[0] + extension

                    filename = unique_archive_name(used_names[extension], filename)
                    archive_path = f"{TYPE_FOLDERS[extension]}/{filename}"

                    with open(temp_path, "rb") as source, archive.open(archive_path, "w") as destination:
                        while True:
                            chunk = source.read(1024 * 256)
                            if not chunk:
                                break
                            destination.write(chunk)

                    downloaded += 1

                except Exception:
                    failed += 1
                finally:
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

        if downloaded == 0:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            return jsonify({
                "error": "Aucun fichier n'a pu être téléchargé. Vérifiez que les fichiers sont publiquement accessibles."
            }), 400

        @after_this_request
        def remove_zip(response):
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except Exception:
                pass
            return response

        return send_file(
            zip_path,
            as_attachment=True,
            download_name="PDF-Hunter-Documents.zip",
            mimetype="application/zip"
        )

    except Exception as exc:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass
        return jsonify({"error": f"Erreur lors de la création du ZIP : {exc}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
