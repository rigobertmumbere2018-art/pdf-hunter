from flask import Flask, render_template, request, jsonify, send_file
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from werkzeug.utils import secure_filename
import requests
import zipfile
import tempfile
import os

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
        return jsonify({
            "error": "Veuillez entrer une URL valide commençant par http:// ou https://"
        }), 400

    try:
        response = requests.get(
            page_url,
            headers=HEADERS,
            timeout=20
        )

        response.raise_for_status()

    except requests.RequestException as e:

        return jsonify({
            "error": f"Impossible d'ouvrir cette page : {str(e)}"
        }), 400


    content_type = response.headers.get(
        "content-type",
        ""
    ).lower()


    if "pdf" in content_type:

        return jsonify({
            "pdfs": [{
                "name": page_url.split("/")[-1] or "document.pdf",
                "url": response.url
            }]
        })


    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )


    found = []

    seen = set()


    for a in soup.find_all("a", href=True):

        href = urljoin(
            response.url,
            a["href"]
        )


        clean = href.split("#")[0]


        if clean.lower().split("?")[0].endswith(".pdf") and clean not in seen:

            seen.add(clean)


            name = a.get_text(
                " ",
                strip=True
            )


            if not name:

                name = clean.split("/")[-1].split("?")[0]

                if not name:
                    name = "document.pdf"


            found.append({

                "name": name[:180],

                "url": clean

            })


    return jsonify({

        "pdfs": found,

        "count": len(found)

    })


@app.post("/download-all")
def download_all():

    data = request.get_json(silent=True) or {}

    pdfs = data.get("pdfs", [])


    if not pdfs:

        return jsonify({
            "error": "Aucun PDF à télécharger."
        }), 400


    if len(pdfs) > 500:

        return jsonify({
            "error": "Maximum 500 PDF par téléchargement."
        }), 400


    temp_file = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".zip"
    )


    temp_path = temp_file.name

    temp_file.close()


    downloaded = 0


    try:

        with zipfile.ZipFile(
            temp_path,
            "w",
            zipfile.ZIP_DEFLATED
        ) as zip_file:


            for index, pdf in enumerate(pdfs, start=1):

                url = pdf.get("url", "")


                if not is_safe_http_url(url):
                    continue


                try:

                    response = requests.get(

                        url,

                        headers=HEADERS,

                        timeout=30

                    )


                    response.raise_for_status()


                    filename = pdf.get(
                        "name",
                        ""
                    )


                    filename = secure_filename(filename)


                    if not filename:

                        filename = f"document_{index}.pdf"


                    if not filename.lower().endswith(".pdf"):

                        filename += ".pdf"


                    zip_file.writestr(

                        filename,

                        response.content

                    )


                    downloaded += 1


                except requests.RequestException:

                    continue


        if downloaded == 0:

            os.remove(temp_path)

            return jsonify({
                "error": "Aucun PDF n'a pu être téléchargé."
            }), 400


        return send_file(

            temp_path,

            as_attachment=True,

            download_name="PDF-Hunter-Documents.zip",

            mimetype="application/zip"

        )


    except Exception as e:

        if os.path.exists(temp_path):

            os.remove(temp_path)


        return jsonify({

            "error": f"Erreur lors de la création du ZIP : {str(e)}"

        }), 500


if __name__ == "__main__":

    app.run(debug=True)
