from flask import Flask, render_template, request, jsonify, send_file, after_this_request
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, parse_qs
from werkzeug.utils import secure_filename
import requests
import zipfile
import tempfile
import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
TYPE_FOLDERS = {".pdf": "PDF", ".jpg": "JPG", ".jpeg": "JPEG", ".png": "PNG"}
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_FILES = 1000
DOWNLOAD_WORKERS = 6
PINTEREST_MAX_PAGES = 5
PINTEREST_PAGE_SIZE = 50


def is_safe_http_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def host_is(url, names):
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == name or host.endswith("." + name) for name in names)


def get_extension_from_url(url):
    path = unquote(urlparse(url).path).lower().rstrip("/")
    for ext in (".jpeg", ".jpg", ".png", ".pdf"):
        if path.endswith(ext):
            return ext
    return ""


def get_extension_from_content_type(content_type):
    content_type = (content_type or "").lower().split(";")[0].strip()
    return {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png"}.get(content_type, "")


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
    name = unquote(name).replace("/", "_").replace("\\", "_")
    name = secure_filename(name)
    return (name or fallback)[:180]


def unique_archive_name(existing, filename):
    base, ext = os.path.splitext(filename)
    candidate = filename
    number = 2
    while candidate.lower() in existing:
        candidate = f"{base}_{number}{ext}"
        number += 1
    existing.add(candidate.lower())
    return candidate


def pinterest_original_candidates(url):
    candidates = [url]
    try:
        parsed = urlparse(url)
        if "pinimg.com" in parsed.netloc.lower():
            match = re.match(r"^/(?:[0-9]+x|736x|564x|474x|236x|170x|75x)/(.+)$", parsed.path, re.I)
            if match:
                candidates.insert(0, parsed._replace(path="/originals/" + match.group(1)).geturl())
    except Exception:
        pass
    return list(dict.fromkeys(candidates))


def download_to_temp(url, referer=""):
    last_error = None
    for candidate in pinterest_original_candidates(url):
        headers = dict(HEADERS)
        if "pinimg.com" in urlparse(candidate).netloc.lower():
            headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        if referer and is_safe_http_url(referer):
            headers["Referer"] = referer
        for _ in range(2):
            temp_path = None
            response = None
            try:
                response = requests.get(candidate, headers=headers, timeout=(15, 60), stream=True, allow_redirects=True)
                response.raise_for_status()
                final_url = response.url
                content_type = response.headers.get("content-type", "")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_FILE_SIZE:
                    raise ValueError("Fichier trop volumineux")
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
                    if response is not None:
                        response.close()
                except Exception:
                    pass
                try:
                    if temp_path and os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
    raise last_error or RuntimeError("Téléchargement impossible")


def add_found(found, seen, url, name=""):
    if not is_safe_http_url(url):
        return
    clean_url = url.split("#")[0].replace("\\/", "/")
    if clean_url in seen:
        return
    extension = get_extension_from_url(clean_url)
    if extension not in ALLOWED_EXTENSIONS:
        return
    seen.add(clean_url)
    name = name or os.path.basename(urlparse(clean_url).path) or f"fichier{extension}"
    if not os.path.splitext(name)[1]:
        name += extension
    found.append({"name": clean_filename(name, f"fichier{extension}"), "url": clean_url, "type": TYPE_FOLDERS[extension], "extension": extension[1:].upper()})


def add_image_candidate(found, seen, url, name=""):
    if not is_safe_http_url(url):
        return
    add_found(found, seen, url, name)


def extract_srcset(value):
    if not value:
        return []
    return [part.strip().split(" ")[0] for part in value.split(",") if part.strip()]


def extract_urls_from_text(text):
    if not text:
        return []
    decoded = text.replace(r"\u002F", "/").replace(r"\/", "/").replace(r"\u003A", ":").replace("&amp;", "&")
    urls = []
    patterns = [
        r'https?://[^"\'<>\\\s]+\.(?:pdf|jpe?g|png)(?:\?[^"\'<>\\\s]*)?',
        r'https?:\\?/\\?/[^"\'<>\\\s]+\.(?:pdf|jpe?g|png)(?:\?[^"\'<>\\\s]*)?',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, decoded, flags=re.I):
            urls.append(match.replace("\\/", "/").rstrip("\\"))
    return urls


def extract_pinterest_urls(text, found, seen):
    if not text:
        return
    decoded = text.replace(r"\u002F", "/").replace(r"\/", "/").replace(r"\u003A", ":")
    pattern = r'https?://i\.pinimg\.com/[^"\'<>\\\s]+'
    for match in re.findall(pattern, decoded, flags=re.I):
        add_found(found, seen, match.rstrip("\\"))


def extract_social_meta(html, response_url, found, seen):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        value = tag.get("content") or ""
        if key in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
            add_found(found, seen, urljoin(response_url, value), "social_image")


def extract_generic_html(html, response_url, found, seen):
    soup = BeautifulSoup(html, "html.parser")
    tags = soup.find_all(["a", "img", "source", "video", "audio", "iframe", "embed", "object", "link"])
    attributes = ["href", "src", "data-src", "data-href", "data-original", "data-url", "data-lazy-src", "data-image-url", "data-fallback-src"]
    for tag in tags:
        possible_urls = []
        for attribute in attributes:
            raw = tag.get(attribute)
            if raw:
                possible_urls.append(raw)
        possible_urls.extend(extract_srcset(tag.get("srcset")))
        possible_urls.extend(extract_srcset(tag.get("data-srcset")))
        for raw_url in possible_urls:
            clean_url = urljoin(response_url, raw_url).split("#")[0]
            name = tag.get("alt") or tag.get_text(" ", strip=True) or os.path.basename(urlparse(clean_url).path)
            add_found(found, seen, clean_url, name)
    extract_social_meta(html, response_url, found, seen)
    extract_pinterest_urls(html, found, seen)
    for match in extract_urls_from_text(html):
        add_found(found, seen, match)


def extract_facebook_image_urls(html, response_url, found, seen):
    """Recover public Facebook preview images when Facebook exposes them in HTML/JSON."""
    if not html:
        return
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        value = tag.get("content") or ""
        if "image" in key and value:
            add_image_candidate(found, seen, urljoin(response_url, value), "facebook_image")
    # Facebook often serializes image URLs inside JSON. Accept known CDN hosts and image extensions.
    decoded = html.replace(r"\u002F", "/").replace(r"\/", "/").replace(r"\u003A", ":")
    patterns = [
        r'https?://(?:scontent[^"\'<>\\\s]+|external[^"\'<>\\\s]+)\.(?:jpe?g|png)(?:\?[^"\'<>\\\s]*)?',
        r'https?://[^"\'<>\\\s]*fbcdn[^"\'<>\\\s]*\.(?:jpe?g|png)(?:\?[^"\'<>\\\s]*)?',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, decoded, flags=re.I):
            add_image_candidate(found, seen, match.rstrip("\\"), "facebook_image")


def facebook_variants(page_url):
    variants = [page_url]
    parsed = urlparse(page_url)
    qs = parse_qs(parsed.query)
    fbid = qs.get("fbid", [""])[0]
    if fbid:
        for host in ("www.facebook.com", "m.facebook.com", "mbasic.facebook.com"):
            variants.append(f"https://{host}/photo.php?fbid={fbid}")
    return list(dict.fromkeys(variants))


def fetch_page(session, page_url):
    """Try the requested URL, then lightweight social-media variants."""
    variants = facebook_variants(page_url) if host_is(page_url, {"facebook.com", "fb.com"}) else [page_url]
    last_error = None
    for variant in variants:
        try:
            response = session.get(variant, timeout=(15, 40), allow_redirects=True)
            # Facebook may answer 400 after redirecting to login. Keep a useful successful variant if one exists.
            if response.status_code < 400:
                return response, ""
            last_error = requests.HTTPError(f"HTTP {response.status_code} pour {response.url}")
        except requests.RequestException as exc:
            last_error = exc
    return None, str(last_error or "Page inaccessible")


def extract_app_version(html):
    for pattern in [r'"appVersion"\s*:\s*"([^"]+)"', r'"app_version"\s*:\s*"([^"]+)"', r'"pws_app_version"\s*:\s*"([^"]+)"']:
        match = re.search(pattern, html or "", flags=re.I)
        if match:
            return match.group(1)
    return ""


def pinterest_resource_search(page_url, session, found, seen):
    if not host_is(page_url, {"pinterest.com", "pinterest.fr", "pinterest.ca"}) or not urlparse(page_url).path.startswith("/search/pins"):
        return 0, ""
    parsed = urlparse(page_url)
    query = parse_qs(parsed.query).get("q", [""])[0].strip()
    if not query:
        return 0, ""
    app_version = extract_app_version(getattr(session, "_pdf_hunter_html", ""))
    csrf = session.cookies.get("csrftoken", "")
    source_url = parsed.path or "/search/pins/"
    if parsed.query:
        source_url += "?" + parsed.query
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": HEADERS["Accept-Language"],
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        "X-NEW-APP": "1",
        "Referer": page_url,
    }
    if app_version:
        headers["X-APP-VERSION"] = app_version
    if csrf:
        headers["X-CSRFToken"] = csrf
    bookmark = None
    total = 0
    for _ in range(PINTEREST_MAX_PAGES):
        options = {"query": query, "scope": "pins", "page_size": PINTEREST_PAGE_SIZE, "field_set_key": "react_grid_pin", "bookmarks": [bookmark] if bookmark else []}
        payload = {"options": options, "context": {}}
        try:
            response = session.get("https://www.pinterest.com/resource/SearchResource/get/", params={"source_url": source_url, "data": json.dumps(payload, separators=(",", ":"))}, headers=headers, timeout=(15, 30), allow_redirects=True)
            if response.status_code in (403, 429):
                return total, f"Pinterest a limité la recherche dynamique (HTTP {response.status_code})."
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return total, f"Recherche dynamique Pinterest interrompue : {exc}"
        resource = body.get("resource_response", {}) if isinstance(body, dict) else {}
        data = resource.get("data", [])
        if isinstance(data, dict):
            data = data.get("results", [])
        if not isinstance(data, list):
            data = []
        before = len(found)
        for pin in data:
            if not isinstance(pin, dict):
                continue
            images = pin.get("images") or {}
            image_url = ""
            for key in ("orig", "736x", "564x", "474x", "236x"):
                value = images.get(key)
                if isinstance(value, dict) and value.get("url"):
                    image_url = value["url"]
                    break
            if image_url:
                pin_id = str(pin.get("id") or "").strip()
                title = pin.get("title") or pin.get("grid_title") or pin.get("alt_text") or ""
                name = clean_filename(title, f"pin_{pin_id or len(found) + 1}.jpg")
                add_found(found, seen, image_url, name)
        total += max(0, len(found) - before)
        next_bookmark = resource.get("bookmark")
        if not next_bookmark or next_bookmark == "-end-" or next_bookmark == bookmark:
            break
        bookmark = next_bookmark
        time.sleep(0.5)
    return total, ""


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/scan")
def scan():
    data = request.get_json(silent=True) or {}
    page_url = (data.get("url") or "").strip()
    if not is_safe_http_url(page_url):
        return jsonify({"error": "Veuillez entrer une URL valide commençant par http:// ou https://"}), 400

    session = requests.Session()
    session.headers.update(HEADERS)
    response, error = fetch_page(session, page_url)
    if response is None:
        if host_is(page_url, {"facebook.com", "fb.com"}):
            return jsonify({"error": "Facebook refuse l'accès automatisé à cette publication depuis le serveur. Essayez avec une page ou une image Facebook publiquement accessible."}), 400
        return jsonify({"error": f"Impossible d'ouvrir cette page : {error}"}), 400

    content_type = response.headers.get("content-type", "")
    page_ext = detect_extension(response.url, content_type, response.content[:32])
    if page_ext in ALLOWED_EXTENSIONS:
        filename = os.path.basename(urlparse(response.url).path) or f"document{page_ext}"
        return jsonify({"files": [{"name": clean_filename(filename, f"document{page_ext}"), "url": response.url, "type": TYPE_FOLDERS[page_ext], "extension": page_ext[1:].upper()}], "count": 1, "source": "direct"})

    html = response.text
    session._pdf_hunter_html = html
    found = []
    seen = set()
    extract_generic_html(html, response.url, found, seen)

    platform_message = ""
    dynamic_count = 0
    if host_is(page_url, {"pinterest.com", "pinterest.fr", "pinterest.ca"}):
        dynamic_count, platform_message = pinterest_resource_search(page_url, session, found, seen)
    elif host_is(page_url, {"facebook.com", "fb.com"}):
        before = len(found)
        extract_facebook_image_urls(html, response.url, found, seen)
        dynamic_count = max(0, len(found) - before)
        if not found:
            platform_message = "Facebook a chargé la publication sans exposer les images au serveur. Les publications privées ou protégées ne peuvent pas être récupérées automatiquement."

    found.sort(key=lambda item: (item["extension"], item["name"].lower()))
    return jsonify({"files": found, "count": len(found), "source": "social" if dynamic_count else "html", "dynamic_count": dynamic_count, "platform_message": platform_message})


def prepare_download(item, index, page_url):
    if not isinstance(item, dict):
        return {"ok": False, "index": index, "error": "Entrée invalide"}
    url = (item.get("url") or "").strip()
    if not is_safe_http_url(url):
        return {"ok": False, "index": index, "error": "URL invalide"}
    temp_path = None
    try:
        temp_path, extension, final_url = download_to_temp(url, page_url)
        requested_name = item.get("name") or os.path.basename(urlparse(final_url).path)
        filename = clean_filename(requested_name, f"fichier_{index}{extension}")
        current_ext = get_extension_from_url(filename)
        if current_ext not in ALLOWED_EXTENSIONS or current_ext != extension:
            filename = os.path.splitext(filename)[0] + extension
        return {"ok": True, "index": index, "temp_path": temp_path, "extension": extension, "filename": filename}
    except Exception as exc:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return {"ok": False, "index": index, "error": str(exc)}


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
    used_names = {ext: set() for ext in ALLOWED_EXTENSIONS}
    temp_paths = []

    try:
        results = []
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
            futures = [executor.submit(prepare_download, item, index, page_url) for index, item in enumerate(files, start=1)]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda result: result.get("index", 0))

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for result in results:
                if not result.get("ok"):
                    failed += 1
                    continue
                temp_path = result["temp_path"]
                temp_paths.append(temp_path)
                extension = result["extension"]
                filename = unique_archive_name(used_names[extension], result["filename"])
                archive_path = f"{TYPE_FOLDERS[extension]}/{filename}"
                try:
                    with open(temp_path, "rb") as source, archive.open(archive_path, "w") as destination:
                        while True:
                            chunk = source.read(1024 * 256)
                            if not chunk:
                                break
                            destination.write(chunk)
                    downloaded += 1
                except Exception:
                    failed += 1

        for temp_path in temp_paths:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

        if downloaded == 0:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            return jsonify({"error": "Aucun fichier n'a pu être téléchargé. Vérifiez que les fichiers sont publiquement accessibles."}), 400

        @after_this_request
        def remove_zip(response):
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except Exception:
                pass
            return response

        response = send_file(zip_path, as_attachment=True, download_name="PDF-Hunter-Documents.zip", mimetype="application/zip")
        response.headers["X-PDF-Hunter-Downloaded"] = str(downloaded)
        response.headers["X-PDF-Hunter-Failed"] = str(failed)
        response.headers["Access-Control-Expose-Headers"] = "X-PDF-Hunter-Downloaded, X-PDF-Hunter-Failed"
        return response
    except Exception as exc:
        for temp_path in temp_paths:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass
        return jsonify({"error": f"Erreur lors de la création du ZIP : {exc}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
