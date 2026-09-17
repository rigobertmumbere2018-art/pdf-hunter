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
    "Pragma": "no-cache",
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
TYPE_FOLDERS = {".pdf": "PDF", ".jpg": "JPG", ".jpeg": "JPEG", ".png": "PNG"}
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".jpg"}
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_FILES = 1000
MAX_SCAN_FILES = 500
DOWNLOAD_WORKERS = 8


def is_safe_http_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def host_of(url):
    return urlparse(url).netloc.lower().split(":")[0]


def is_social_host(url):
    host = host_of(url)
    return any(x in host for x in (
        "facebook.com", "fb.com", "instagram.com", "threads.net", "pinterest.",
        "tiktok.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
        "youtu.be", "snapchat.com", "reddit.com", "flickr.com", "tumblr.com",
        "whatsapp.com"
    ))


def get_extension_from_url(url):
    path = unquote(urlparse(url).path).lower().rstrip("/")
    for ext in (".jpeg", ".jpg", ".png", ".pdf"):
        if path.endswith(ext):
            return ext
    return ""


def get_extension_from_content_type(content_type):
    content_type = (content_type or "").lower().split(";")[0].strip()
    return {"application/pdf": ".pdf", **IMAGE_TYPES}.get(content_type, "")


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
    if first_bytes[:12] == b"RIFF" + first_bytes[4:8] + b"WEBP":
        return ".jpg"
    return ""


def clean_filename(name, fallback):
    name = unquote((name or "").strip()).replace("/", "_").replace("\\", "_")
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


def add_found(found, seen, url, name="", force_ext=""):
    if not is_safe_http_url(url):
        return False
    clean_url = url.split("#", 1)[0].replace("\\/", "/")
    key = clean_url.lower()
    if key in seen or len(found) >= MAX_SCAN_FILES:
        return False
    ext = force_ext or get_extension_from_url(clean_url)
    if ext not in ALLOWED_EXTENSIONS:
        return False
    seen.add(key)
    name = name or os.path.basename(urlparse(clean_url).path) or f"fichier_{len(found)+1}{ext}"
    if not os.path.splitext(name)[1]:
        name += ext
    found.append({
        "name": clean_filename(name, f"fichier_{len(found)+1}{ext}"),
        "url": clean_url,
        "type": TYPE_FOLDERS[ext],
        "extension": ext[1:].upper(),
    })
    return True


def extract_srcset(value):
    if not value:
        return []
    return [part.strip().split()[0] for part in value.split(",") if part.strip()]


def extract_url_candidates(text):
    if not text:
        return []
    decoded = text.replace(r"\u002F", "/").replace(r"\/", "/").replace(r"\u003A", ":").replace("&amp;", "&")
    patterns = [
        r'https?://[^\"\'<>\s]+\.(?:pdf|jpe?g|png)(?:\?[^\"\'<>\s]*)?',
        r'https?://i\.pinimg\.com/[^\"\'<>\\\s]+',
        r'https?://[^\"\'<>\s]*(?:fbcdn|cdninstagram|tiktokcdn|twimg|licdn|ytimg)[^\"\'<>\s]*',
    ]
    result = []
    for pattern in patterns:
        result.extend(re.findall(pattern, decoded, flags=re.I))
    return result


def extract_json_images(obj, found, seen, limit=200):
    if len(found) >= MAX_SCAN_FILES or limit <= 0:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if isinstance(value, str) and ("image" in key_l or key_l in {"src", "url", "original", "image_url", "thumbnail_url", "contenturl"}):
                for candidate in extract_url_candidates(value):
                    add_found(found, seen, candidate)
            else:
                extract_json_images(value, found, seen, limit - 1)
    elif isinstance(obj, list):
        for value in obj[:200]:
            extract_json_images(value, found, seen, limit - 1)


def extract_generic_html(html, response_url, found, seen):
    soup = BeautifulSoup(html or "", "html.parser")

    # OpenGraph/Twitter metadata is especially useful on Facebook, Instagram, TikTok, X, LinkedIn and Reddit.
    meta_values = []
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        value = meta.get("content")
        if value and any(x in key for x in ("og:image", "twitter:image", "image", "thumbnail")):
            meta_values.append(value)
    for value in meta_values:
        candidate = urljoin(response_url, value)
        add_found(found, seen, candidate)

    # JSON-LD structured data.
    for script in soup.find_all("script", type=lambda x: x and "ld+json" in x):
        try:
            obj = json.loads(script.string or script.get_text())
            extract_json_images(obj, found, seen)
        except Exception:
            for candidate in extract_url_candidates(script.get_text()):
                add_found(found, seen, candidate)

    tags = soup.find_all(["a", "img", "source", "video", "audio", "iframe", "embed", "object", "link"])
    attrs = ["href", "src", "data-src", "data-href", "data-original", "data-url", "data-lazy-src", "data-image-url", "data-thumbnail"]
    for tag in tags:
        possible = []
        for attr in attrs:
            raw = tag.get(attr)
            if raw:
                possible.append(raw)
        possible.extend(extract_srcset(tag.get("srcset")))
        possible.extend(extract_srcset(tag.get("data-srcset")))
        for raw in possible:
            candidate = urljoin(response_url, raw).split("#", 1)[0]
            name = tag.get("alt") or tag.get("title") or tag.get_text(" ", strip=True) or os.path.basename(urlparse(candidate).path)
            add_found(found, seen, candidate, name)

    # CSS inline/background URLs and serialized application state.
    for style in soup.find_all("style"):
        for candidate in re.findall(r'url\([\"\']?([^\"\')]+)', style.get_text(), flags=re.I):
            add_found(found, seen, urljoin(response_url, candidate))

    for candidate in extract_url_candidates(html):
        add_found(found, seen, candidate)


def social_message(url, html=""):
    host = host_of(url)
    low = (html or "").lower()
    if "whatsapp.com" in host:
        return "WhatsApp Web nécessite une session connectée dans un navigateur : un serveur public ne peut pas lire votre session WhatsApp."
    if "facebook.com" in host or "fb.com" in host:
        if "login" in low or "checkpoint" in low or "log in" in low:
            return "Facebook protège cette page derrière une connexion. Seules les images publiquement exposées par la page peuvent être récupérées."
    if "instagram.com" in host and ("login" in low or "log in" in low):
        return "Instagram protège cette page derrière une connexion. Seules les données publiques exposées sans connexion peuvent être récupérées."
    if "linkedin.com" in host and "sign in" in low:
        return "LinkedIn demande une connexion pour cette page."
    return ""


def try_fetch_page(session, page_url):
    variants = [page_url]
    parsed = urlparse(page_url)
    host = parsed.netloc.lower()

    # Facebook sometimes responds differently on mobile hosts for public pages.
    if "facebook.com" in host:
        path_query = parsed.path + (("?" + parsed.query) if parsed.query else "")
        variants += [
            "https://m.facebook.com" + path_query,
            "https://mbasic.facebook.com" + path_query,
        ]

    # X/Twitter public pages can expose metadata through x.com/twitter.com interchangeably.
    if "twitter.com" in host:
        variants.append("https://x.com" + parsed.path + (("?" + parsed.query) if parsed.query else ""))
    if host == "x.com" or host.endswith(".x.com"):
        variants.append("https://twitter.com" + parsed.path + (("?" + parsed.query) if parsed.query else ""))

    errors = []
    for variant in dict.fromkeys(variants):
        try:
            response = session.get(variant, timeout=(15, 40), allow_redirects=True)
            if response.status_code < 400:
                return response, errors
            errors.append(f"HTTP {response.status_code} sur {host_of(variant)}")
        except requests.RequestException as exc:
            errors.append(str(exc))
    return None, errors


def tiktok_oembed(session, page_url, found, seen):
    if "tiktok.com" not in host_of(page_url):
        return 0
    try:
        response = session.get("https://www.tiktok.com/oembed", params={"url": page_url}, timeout=(10, 20), headers={"Accept": "application/json"})
        if response.ok:
            data = response.json()
            image = data.get("thumbnail_url")
            if image:
                return 1 if add_found(found, seen, image, data.get("title") or "TikTok") else 0
    except Exception:
        pass
    return 0


def youtube_fallback(page_url, found, seen):
    host = host_of(page_url)
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        return 0
    parsed = urlparse(page_url)
    video_id = ""
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if not video_id:
            m = re.search(r"/shorts/([^/?]+)", parsed.path)
            if m:
                video_id = m.group(1)
    if video_id:
        return 1 if add_found(found, seen, f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg", f"youtube_{video_id}.jpg") else 0
    return 0


def pinterest_resource_search(page_url, session, found, seen):
    if "pinterest." not in host_of(page_url) or not urlparse(page_url).path.startswith("/search/pins"):
        return 0, ""
    query = parse_qs(urlparse(page_url).query).get("q", [""])[0].strip()
    if not query:
        return 0, ""

    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": HEADERS["Accept-Language"],
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
        "Referer": page_url,
    }
    bookmark = None
    total = 0
    for _ in range(5):
        options = {"query": query, "scope": "pins", "page_size": 50, "field_set_key": "react_grid_pin", "bookmarks": [bookmark] if bookmark else []}
        params = {"source_url": "/search/pins/?q=" + query, "data": json.dumps({"options": options, "context": {}}, separators=(",", ":"))}
        try:
            response = session.get("https://www.pinterest.com/resource/SearchResource/get/", params=params, headers=headers, timeout=(15, 30))
            if response.status_code in (403, 429):
                return total, f"Pinterest a limité la recherche automatique (HTTP {response.status_code})."
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            return total, f"Recherche Pinterest interrompue : {exc}"

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
                title = pin.get("title") or pin.get("grid_title") or pin.get("alt_text") or ""
                pin_id = str(pin.get("id") or len(found) + 1)
                add_found(found, seen, image_url, clean_filename(title, f"pin_{pin_id}.jpg"))
        total += len(found) - before
        next_bookmark = resource.get("bookmark")
        if not next_bookmark or next_bookmark in {"-end-", bookmark}:
            break
        bookmark = next_bookmark
        time.sleep(0.4)
    return total, ""


def scan_page(page_url):
    session = requests.Session()
    session.headers.update(HEADERS)
    response, errors = try_fetch_page(session, page_url)
    if response is None:
        # Do not return a raw 400 to the user: explain the limitation.
        message = "Impossible d'ouvrir cette page depuis le serveur."
        if is_social_host(page_url):
            message += " Le réseau social peut exiger une session, bloquer les serveurs cloud ou charger son contenu uniquement en JavaScript."
        return [], message, errors

    content_type = response.headers.get("content-type", "")
    page_ext = detect_extension(response.url, content_type, response.content[:32])
    if page_ext in ALLOWED_EXTENSIONS:
        filename = os.path.basename(urlparse(response.url).path) or f"document{page_ext}"
        return [{"name": clean_filename(filename, f"document{page_ext}"), "url": response.url, "type": TYPE_FOLDERS[page_ext], "extension": page_ext[1:].upper()}], "", []

    html = response.text or ""
    found = []
    seen = set()
    extract_generic_html(html, response.url, found, seen)

    extra = 0
    pinterest_message = ""
    extra += tiktok_oembed(session, page_url, found, seen)
    extra += youtube_fallback(page_url, found, seen)
    if "pinterest." in host_of(page_url):
        count, pinterest_message = pinterest_resource_search(page_url, session, found, seen)
        extra += count

    message = pinterest_message or social_message(response.url, html)
    if not found and is_social_host(page_url) and not message:
        message = "Cette page semble charger son contenu dynamiquement ou derrière une session. PDF Hunter ne peut récupérer que les médias que le site expose publiquement au serveur."

    found.sort(key=lambda x: (x["extension"], x["name"].lower()))
    return found[:MAX_SCAN_FILES], message, errors


def download_to_temp(url, referer=""):
    candidates = [url]
    host = host_of(url)
    if "pinimg.com" in host:
        parsed = urlparse(url)
        match = re.match(r"^/(?:[0-9]+x|736x|564x|474x|236x|170x|75x)/(.+)$", parsed.path, re.I)
        if match:
            candidates.insert(0, parsed._replace(path="/originals/" + match.group(1)).geturl())
    last_error = None
    for candidate in dict.fromkeys(candidates):
        headers = dict(HEADERS)
        if referer and is_safe_http_url(referer):
            headers["Referer"] = referer
        if "pinimg.com" in host_of(candidate):
            headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        try:
            response = requests.get(candidate, headers=headers, timeout=(15, 60), stream=True, allow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            temp = tempfile.NamedTemporaryFile(delete=False, suffix=".download")
            temp_path = temp.name
            total = 0
            first_bytes = b""
            try:
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
                extension = detect_extension(response.url, content_type, first_bytes)
                if extension not in ALLOWED_EXTENSIONS:
                    raise ValueError("Type de fichier non pris en charge")
                return temp_path, extension, response.url
            except Exception:
                temp.close()
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise
            finally:
                response.close()
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("Téléchargement impossible")


def prepare_download(item, index, page_url):
    if not isinstance(item, dict):
        return {"ok": False, "index": index}
    url = (item.get("url") or "").strip()
    if not is_safe_http_url(url):
        return {"ok": False, "index": index}
    temp_path = None
    try:
        temp_path, extension, final_url = download_to_temp(url, page_url)
        requested = item.get("name") or os.path.basename(urlparse(final_url).path)
        filename = clean_filename(requested, f"fichier_{index}{extension}")
        current = get_extension_from_url(filename)
        if current != extension:
            filename = os.path.splitext(filename)[0] + extension
        return {"ok": True, "index": index, "temp_path": temp_path, "extension": extension, "filename": filename}
    except Exception as exc:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        return {"ok": False, "index": index, "error": str(exc)}


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/scan")
def scan():
    data = request.get_json(silent=True) or {}
    page_url = (data.get("url") or "").strip()
    if not is_safe_http_url(page_url):
        return jsonify({"error": "Veuillez entrer une URL valide commençant par http:// ou https://"}), 400
    files, message, errors = scan_page(page_url)
    return jsonify({"files": files, "count": len(files), "message": message, "network_errors": errors[:3], "social": is_social_host(page_url)})


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
    results = []
    temp_paths = []
    downloaded = 0
    failed = 0
    used_names = {ext: set() for ext in ALLOWED_EXTENSIONS}

    try:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
            futures = [executor.submit(prepare_download, item, i, page_url) for i, item in enumerate(files, 1)]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda x: x.get("index", 0))

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for result in results:
                if not result.get("ok"):
                    failed += 1
                    continue
                temp_path = result["temp_path"]
                temp_paths.append(temp_path)
                ext = result["extension"]
                filename = unique_archive_name(used_names[ext], result["filename"])
                try:
                    with open(temp_path, "rb") as source, archive.open(f"{TYPE_FOLDERS[ext]}/{filename}", "w") as dest:
                        while True:
                            chunk = source.read(1024 * 256)
                            if not chunk:
                                break
                            dest.write(chunk)
                    downloaded += 1
                except Exception:
                    failed += 1

        for path in temp_paths:
            if os.path.exists(path):
                os.remove(path)
        if downloaded == 0:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            return jsonify({"error": "Aucun fichier n'a pu être téléchargé. Certains réseaux sociaux bloquent les téléchargements depuis les serveurs cloud."}), 400

        @after_this_request
        def cleanup(response):
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
        for path in temp_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return jsonify({"error": f"Erreur lors de la création du ZIP : {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
