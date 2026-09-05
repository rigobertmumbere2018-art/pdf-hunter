from flask import Flask, render_template, request, jsonify
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import requests

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PDF-Hunter/1.0)"
}

def is_safe_http_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

@app.route("/")
def index():
    return render_template("index.html")

@app.post("/scan")
def scan():
    data = request.get_json(silent=True) or {}
    page_url = (data.get("url") or "").strip()

    if not is_safe_http_url(page_url):
        return jsonify({"error": "Veuillez entrer une URL valide commençant par http:// ou https://"}), 400

    try:
        response = requests.get(page_url, headers=HEADERS, timeout=20)
        response.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Impossible d'ouvrir cette page : {str(e)}"}), 400

    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type:
        return jsonify({
            "pdfs": [{
                "name": page_url.split("/")[-1] or "document.pdf",
                "url": response.url
            }]
        })

    soup = BeautifulSoup(response.text, "html.parser")
    found = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(response.url, a["href"])
        clean = href.split("#")[0]

        if clean.lower().split("?")[0].endswith(".pdf") and clean not in seen:
            seen.add(clean)
            name = a.get_text(" ", strip=True)
            if not name:
                name = clean.split("/")[-1].split("?")[0] or "document.pdf"

            found.append({
                "name": name[:180],
                "url": clean
            })

    return jsonify({"pdfs": found, "count": len(found)})

if __name__ == "__main__":
    app.run(debug=True)
