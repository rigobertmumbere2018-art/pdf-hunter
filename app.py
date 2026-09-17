from flask import Flask, render_template, request, jsonify, send_file, after_this_request
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
from werkzeug.utils import secure_filename
import requests, zipfile, tempfile, os, re, socket, ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
TYPE_FOLDERS = {".pdf":"PDF", ".jpg":"JPG", ".jpeg":"JPEG", ".png":"PNG", ".webp":"WEBP", ".gif":"GIF", ".svg":"SVG"}
MIME_EXTENSIONS = {
    "application/pdf":".pdf", "image/jpeg":".jpg", "image/jpg":".jpg", "image/png":".png",
    "image/webp":".webp", "image/gif":".gif", "image/svg+xml":".svg"
}
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_FILES = 1000
MAX_CRAWL_PAGES = 60
MAX_CRAWL_DEPTH = 2
DOWNLOAD_WORKERS = 8


def is_safe_http_url(url):
    try:
        p = urlparse(url)
        if p.scheme.lower() not in ("http", "https") or not p.netloc:
            return False
        host = p.hostname
        if not host:
            return False
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False
        except Exception:
            pass
        return True
    except Exception:
        return False


def ext_from_url(url):
    path = unquote(urlparse(url).path).lower().rstrip("/")
    for ext in (".jpeg", ".jpg", ".png", ".webp", ".gif", ".svg", ".pdf"):
        if path.endswith(ext):
            return ext
    return ""


def ext_from_type(content_type):
    return MIME_EXTENSIONS.get((content_type or "").lower().split(";")[0].strip(), "")


def detect_ext(url="", content_type="", first=b""):
    ext = ext_from_url(url) or ext_from_type(content_type)
    if ext:
        return ext
    if first.startswith(b"%PDF"):
        return ".pdf"
    if first.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if first.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if first[:4] == b"RIFF" and first[8:12] == b"WEBP":
        return ".webp"
    if first.startswith(b"GIF8"):
        return ".gif"
    if b"<svg" in first[:1000].lower() or first.lstrip().startswith(b"<?xml"):
        return ".svg"
    return ""


def clean_filename(name, fallback):
    name = unquote(name or "").strip().replace("/", "_").replace("\\", "_")
    name = secure_filename(name)
    return (name or fallback)[:180]


def add_found(found, seen, url, name="", source=""):
    if not is_safe_http_url(url):
        return
    url = url.split("#")[0].strip().replace("\\/", "/")
    if url in seen:
        return
    ext = ext_from_url(url)
    if ext not in ALLOWED_EXTENSIONS:
        return
    seen.add(url)
    base = os.path.basename(urlparse(url).path) or ("fichier" + ext)
    filename = clean_filename(name or base, "fichier" + ext)
    if not os.path.splitext(filename)[1]:
        filename += ext
    found.append({"name": filename, "url": url, "type": TYPE_FOLDERS[ext], "extension": ext[1:].upper(), "source": source})


def srcset_urls(value):
    if not value:
        return []
    out = []
    for part in value.split(","):
        bits = part.strip().split()
        if bits:
            out.append(bits[0])
    return out


def extract_urls_from_text(text):
    if not text:
        return []
    decoded = text.replace(r"\u002F", "/").replace(r"\/", "/").replace(r"\u003A", ":").replace("&amp;", "&")
    pattern = r'https?://[^"\'<>\s\\]+?\.(?:jpe?g|png|webp|gif|svg|pdf)(?:\?[^"\'<>\s\\]*)?'
    return re.findall(pattern, decoded, re.I)


def extract_page(html, base_url, found, seen):
    soup = BeautifulSoup(html, "html.parser")
    tags = soup.find_all(["img", "source", "picture", "video", "audio", "a", "link", "meta", "object", "embed"])
    attrs = ["src", "data-src", "data-original", "data-lazy-src", "data-url", "data-image-url", "data-image", "data-fallback-src", "data-thumb", "data-full", "href"]

    for tag in tags:
        values = []
        for attr in attrs:
            value = tag.get(attr)
            if value:
                values.append(value)
        values += srcset_urls(tag.get("srcset"))
        values += srcset_urls(tag.get("data-srcset"))
        if tag.name == "meta" and tag.get("content"):
            values.append(tag.get("content"))
        for raw in values:
            url = urljoin(base_url, raw)
            name = tag.get("alt") or tag.get("title") or tag.get_text(" ", strip=True)
            add_found(found, seen, url, name, "HTML")

    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            add_found(found, seen, urljoin(base_url, meta.get("content", "")), "", "META")

    for raw in extract_urls_from_text(html):
        add_found(found, seen, urljoin(base_url, raw), "", "JSON")

    for raw in re.findall(r'url\(\s*[\'\"]?([^\'\")]+)[\'\"]?\s*\)', html, re.I):
        add_found(found, seen, urljoin(base_url, raw), "", "CSS")

    links = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0]
        if is_safe_http_url(url):
            links.append((url, a.get_text(" ", strip=True)))
    return links


def likely_detail_link(url, text=""):
    path = urlparse(url).path.lower()
    if any(x in path for x in ("/login", "/signup", "/register", "/privacy", "/terms", "/contact", "/account")):
        return False
    return bool(text.strip()) or any(x in path for x in ("/png/", "/image/", "/images/", "/photo/", "/pin/", "/post/", "/item/", "/download/", "/search"))


def crawl_site(page_url):
    session = requests.Session()
    session.headers.update(HEADERS)
    found, seen_files, visited = [], set(), set()
    queue = [(page_url, 0)]
    root_host = (urlparse(page_url).hostname or "").lower()

    while queue and len(visited) < MAX_CRAWL_PAGES and len(found) < MAX_FILES:
        url, depth = queue.pop(0)
        if url in visited or depth > MAX_CRAWL_DEPTH:
            continue
        if (urlparse(url).hostname or "").lower() != root_host:
            continue
        visited.add(url)
        try:
            response = session.get(url, timeout=(12, 30), allow_redirects=True)
            if response.status_code >= 400:
                continue
            content_type = response.headers.get("content-type", "")
            direct_ext = detect_ext(response.url, content_type, response.content[:64])
            if direct_ext in ALLOWED_EXTENSIONS:
                add_found(found, seen_files, response.url, "", "DIRECT")
                continue
            if "html" not in content_type.lower() and "text" not in content_type.lower():
                continue
            links = extract_page(response.text, response.url, found, seen_files)
            if depth < MAX_CRAWL_DEPTH:
                candidates = []
                for link, text in links:
                    if link in visited or urlparse(link).hostname != urlparse(response.url).hostname:
                        continue
                    low = link.lower()
                    score = 0
                    if any(k in low for k in ("search", "page=", "page/", "?p=", "/p/", "/png/", "/image/", "/photo/", "/pin/")):
                        score += 3
                    if likely_detail_link(link, text):
                        score += 1
                    if score:
                        candidates.append((score, link))
                candidates.sort(reverse=True)
                for _, link in candidates[:35]:
                    if link not in visited:
                        queue.append((link, depth + 1))
        except Exception:
            continue
    return found, len(visited)


def pinterest_candidates(url):
    candidates = [url]
    p = urlparse(url)
    if "pinimg.com" in p.netloc.lower():
        match = re.match(r"^/(?:[0-9]+x|736x|564x|474x|236x|170x|75x)/(.+)$", p.path, re.I)
        if match:
            candidates.insert(0, p._replace(path="/originals/" + match.group(1)).geturl())
    return list(dict.fromkeys(candidates))


def download_to_temp(url, referer=""):
    last_error = None
    for candidate in pinterest_candidates(url):
        try:
            headers = dict(HEADERS)
            headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
            if referer and is_safe_http_url(referer):
                headers["Referer"] = referer
            response = requests.get(candidate, headers=headers, timeout=(15, 60), stream=True, allow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            total = 0
            first = b""
            temp = tempfile.NamedTemporaryFile(delete=False, suffix=".download")
            path = temp.name
            for chunk in response.iter_content(262144):
                if not chunk:
                    continue
                if not first:
                    first = chunk[:64]
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    raise ValueError("Fichier trop volumineux")
                temp.write(chunk)
            temp.close()
            response.close()
            ext = detect_ext(response.url, content_type, first)
            if ext not in ALLOWED_EXTENSIONS:
                raise ValueError("Type de fichier non pris en charge")
            return path, ext, response.url
        except Exception as exc:
            last_error = exc
            try:
                if 'path' in locals() and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
    raise last_error or RuntimeError("Téléchargement impossible")


def prepare_download(item, index, page_url):
    try:
        path, ext, final_url = download_to_temp(item.get("url", ""), page_url)
        name = clean_filename(item.get("name") or os.path.basename(urlparse(final_url).path), f"fichier_{index}{ext}")
        if ext_from_url(name) != ext:
            name = os.path.splitext(name)[0] + ext
        return {"ok": True, "index": index, "path": path, "ext": ext, "name": name}
    except Exception as exc:
        return {"ok": False, "index": index, "error": str(exc)}


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/scan")
def scan():
    data = request.get_json(silent=True) or {}
    page_url = (data.get("url") or "").strip()
    if not is_safe_http_url(page_url):
        return jsonify({"error": "URL invalide ou non accessible."}), 400
    try:
        found, pages = crawl_site(page_url)
    except Exception as exc:
        return jsonify({"error": f"Analyse impossible : {exc}"}), 500

    host = (urlparse(page_url).hostname or "").lower()
    platform_message = f"{pages} page(s) analysée(s)."
    if any(x in host for x in ("facebook.", "instagram.", "tiktok.", "whatsapp.", "pinterest.")):
        platform_message += " Ce réseau peut limiter le contenu visible sans connexion."
    if "pngegg.com" in host:
        platform_message += " PNGEgg est analysé avec les pages d'images et les ressources intégrées."

    found = found[:MAX_FILES]
    return jsonify({
        "files": found,
        "count": len(found),
        "pages_scanned": pages,
        "platform_message": platform_message
    })


@app.post("/download-all")
def download_all():
    data = request.get_json(silent=True) or {}
    files = data.get("files") or []
    page_url = (data.get("page_url") or "").strip()
    if not isinstance(files, list) or not files:
        return jsonify({"error": "Aucun fichier à télécharger."}), 400
    files = files[:MAX_FILES]

    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    zip_path = temp_zip.name
    temp_zip.close()
    results, temp_paths = [], []
    downloaded = failed = 0

    try:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
            futures = [executor.submit(prepare_download, item, index, page_url) for index, item in enumerate(files, 1)]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item["index"])
        used = {ext: set() for ext in ALLOWED_EXTENSIONS}

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for result in results:
                if not result["ok"]:
                    failed += 1
                    continue
                path = result["path"]
                temp_paths.append(path)
                name = result["name"]
                base, ext = os.path.splitext(name)
                candidate = name
                number = 2
                while candidate.lower() in used[result["ext"]]:
                    candidate = f"{base}_{number}{ext}"
                    number += 1
                used[result["ext"]].add(candidate.lower())
                try:
                    with open(path, "rb") as source, archive.open(f"{TYPE_FOLDERS[result['ext']]}/{candidate}", "w") as destination:
                        while True:
                            chunk = source.read(262144)
                            if not chunk:
                                break
                            destination.write(chunk)
                    downloaded += 1
                except Exception:
                    failed += 1

        for path in temp_paths:
            try:
                os.remove(path)
            except Exception:
                pass

        if downloaded == 0:
            try:
                os.remove(zip_path)
            except Exception:
                pass
            return jsonify({"error": "Aucun fichier n'a pu être téléchargé."}), 400

        @after_this_request
        def cleanup(response):
            try:
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
                os.remove(path)
            except Exception:
                pass
        try:
            os.remove(zip_path)
        except Exception:
            pass
        return jsonify({"error": f"Erreur ZIP : {exc}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
