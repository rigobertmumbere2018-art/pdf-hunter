from flask import Flask, render_template, request, jsonify, send_file
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, zipfile, tempfile, os, re, socket, ipaddress

app = Flask(__name__)
HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36","Accept-Language":"fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"}
EXTS={".pdf",".jpg",".jpeg",".png",".webp",".gif",".svg"}
FOLDERS={".pdf":"PDF",".jpg":"JPG",".jpeg":"JPEG",".png":"PNG",".webp":"WEBP",".gif":"GIF",".svg":"SVG"}
MIMES={"application/pdf":".pdf","image/jpeg":".jpg","image/jpg":".jpg","image/png":".png","image/webp":".webp","image/gif":".gif","image/svg+xml":".svg"}
MAX_FILE=100*1024*1024; MAX_FILES=1000; MAX_PAGES=80; MAX_DEPTH=2

def safe_url(url):
    try:
        p=urlparse(url)
        if p.scheme not in ("http","https") or not p.hostname:return False
        try:
            for x in socket.getaddrinfo(p.hostname,None):
                ip=ipaddress.ip_address(x[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:return False
        except Exception:pass
        return True
    except Exception:return False

def ext_url(url):
    path=unquote(urlparse(url).path).lower()
    for e in (".jpeg",".jpg",".png",".webp",".gif",".svg",".pdf"):
        if path.endswith(e):return e
    return ""

def ext_type(ct):return MIMES.get((ct or "").split(";",1)[0].strip().lower(),"")

def detect(url="",ct="",first=b""):
    e=ext_url(url) or ext_type(ct)
    if e:return e
    if first.startswith(b"%PDF"):return ".pdf"
    if first.startswith(b"\x89PNG\r\n\x1a\n"):return ".png"
    if first.startswith(b"\xff\xd8\xff"):return ".jpg"
    if first[:4]==b"RIFF" and first[8:12]==b"WEBP":return ".webp"
    if first.startswith(b"GIF8"):return ".gif"
    if b"<svg" in first[:2000].lower():return ".svg"
    return ""

def filename(name,fallback):
    name=secure_filename(unquote(name or "").replace("/","_").replace("\\","_")).strip()
    return (name or fallback)[:180]

def add(found,seen,url,name="",base=""):
    if not url:return
    url=urljoin(base or "https://example.com/",url).split("#",1)[0].strip().replace("\\/","/")
    if not safe_url(url) or url in seen:return
    e=ext_url(url)
    if e not in EXTS:return
    seen.add(url); n=filename(name or os.path.basename(urlparse(url).path),"fichier"+e)
    if not ext_url(n):n+=e
    found.append({"name":n,"url":url,"type":FOLDERS[e],"extension":e[1:].upper(),"source":"HTML"})

def srcset(v):return [x.strip().split()[0] for x in (v or "").split(",") if x.strip()]

def extract(html,base,found,seen):
    soup=BeautifulSoup(html,"html.parser")
    attrs=["src","href","data-src","data-original","data-lazy-src","data-url","data-image-url","data-image","data-fallback-src","data-thumb","data-full","data-original-src"]
    for tag in soup.find_all(True):
        vals=[]
        for a in attrs:
            if tag.get(a):vals.append(tag.get(a))
        vals+=srcset(tag.get("srcset"))+srcset(tag.get("data-srcset"))
        for v in vals:add(found,seen,v,tag.get("alt") or tag.get("title"),base)
    for m in soup.find_all("meta"):
        p=(m.get("property") or m.get("name") or "").lower()
        if p in ("og:image","og:image:url","twitter:image","twitter:image:src"):add(found,seen,m.get("content",""),"",base)
    text=html.replace(r"\/","/").replace(r"\u002F","/").replace("&amp;","&")
    pattern=r'https?://[^"\'<>\s\\]+?\.(?:jpe?g|png|webp|gif|svg|pdf)(?:\?[^"\'<>\s\\]*)?'
    for u in re.findall(pattern,text,re.I):add(found,seen,u,"",base)
    for u in re.findall(r'url\(\s*["\']?([^"\')]+)',html,re.I):add(found,seen,u,"",base)
    links=[]
    for a in soup.find_all("a",href=True):
        u=urljoin(base,a["href"]).split("#",1)[0]
        if safe_url(u):links.append((u,a.get_text(" ",strip=True)))
    return links

def jina_fetch(url):
    r=requests.get("https://r.jina.ai/"+url,headers={**HEADERS,"Accept":"text/plain"},timeout=(15,45));r.raise_for_status();return r.text

def get_page(url):
    r=requests.get(url,headers=HEADERS,timeout=(12,35),allow_redirects=True);ct=r.headers.get("content-type","")
    if r.status_code<400 and ("html" in ct.lower() or "text" in ct.lower()):return r.text,r.url
    raise requests.HTTPError(f"HTTP {r.status_code}")

def follow(url,text):
    p=urlparse(url).path.lower()
    if any(x in p for x in ("/login","/signup","/register","/privacy","/terms","/account","/contact")):return False
    return bool(text.strip()) or any(x in (p+url.lower()) for x in ("/png/","/image","/photo","/pin/","/post/","/item/","/search","page=","page/"))

def crawl(start):
    root=(urlparse(start).hostname or "").lower();found=[];seen=set();visited=set();queue=[(start,0)];proxy=False
    while queue and len(visited)<MAX_PAGES and len(found)<MAX_FILES:
        url,depth=queue.pop(0)
        if url in visited or depth>MAX_DEPTH or (urlparse(url).hostname or "").lower()!=root:continue
        visited.add(url)
        try:
            try:html,final=get_page(url)
            except Exception:html,final=jina_fetch(url),url;proxy=True
            extract(html,final,found,seen)
            if depth>=MAX_DEPTH:continue
            links=extract(html,final,[],set()); candidates=[]
            for link,text in links:
                if (urlparse(link).hostname or "").lower()!=root or link in visited:continue
                if follow(link,text):
                    low=link.lower();score=(4 if "search" in low or "page" in low else 0)+(3 if any(x in low for x in ("/png/","/image","/photo","/pin/")) else 0)
                    candidates.append((score,link))
            for _,link in sorted(set(candidates),reverse=True)[:40]:
                if link not in visited:queue.append((link,depth+1))
        except Exception:continue
    return found[:MAX_FILES],len(visited),proxy

def download(url,referer=""):
    h=dict(HEADERS);h["Accept"]="image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    if safe_url(referer):h["Referer"]=referer
    r=requests.get(url,headers=h,timeout=(15,70),stream=True,allow_redirects=True);r.raise_for_status();ct=r.headers.get("content-type","")
    f=tempfile.NamedTemporaryFile(delete=False,suffix=".download");total=0;first=b""
    try:
        for chunk in r.iter_content(262144):
            if not chunk:continue
            if not first:first=chunk[:64]
            total+=len(chunk)
            if total>MAX_FILE:raise ValueError("Fichier trop volumineux")
            f.write(chunk)
        f.close();e=detect(r.url,ct,first)
        if e not in EXTS:raise ValueError("Type de fichier non pris en charge")
        return f.name,e,r.url
    except Exception:
        f.close()
        try:os.remove(f.name)
        except Exception:pass
        raise

def one(item,i,referer):
    try:
        path,e,final=download(item["url"],referer);n=filename(item.get("name") or os.path.basename(urlparse(final).path),f"fichier_{i}{e}")
        if ext_url(n)!=e:n=os.path.splitext(n)[0]+e
        return {"ok":True,"path":path,"ext":e,"name":n,"i":i}
    except Exception as ex:return {"ok":False,"i":i,"error":str(ex)}

@app.route("/")
def home():return render_template("index.html")

@app.post("/scan")
def scan():
    data=request.get_json(silent=True) or {};url=(data.get("url") or "").strip()
    if not safe_url(url):return jsonify({"error":"URL invalide ou non accessible."}),400
    try:files,pages,proxy=crawl(url)
    except Exception as e:return jsonify({"error":f"Analyse impossible : {e}"}),500
    host=(urlparse(url).hostname or "").lower();msg=f"{pages} page(s) analysée(s)."
    if proxy:msg+=" Mode compatibilité activé pour cette page."
    if "pngegg.com" in host:msg+=" PNGEgg : images PNG et CDN e7.pngegg.com détectés."
    return jsonify({"files":files,"count":len(files),"pages_scanned":pages,"platform_message":msg})

@app.post("/download-all")
def download_all():
    data=request.get_json(silent=True) or {};files=data.get("files") or [];referer=(data.get("page_url") or "").strip()
    if not isinstance(files,list) or not files:return jsonify({"error":"Aucun fichier à télécharger."}),400
    files=files[:MAX_FILES];z=tempfile.NamedTemporaryFile(delete=False,suffix=".zip");z.close();results=[];temp=[];ok=bad=0
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            fs=[ex.submit(one,x,i,referer) for i,x in enumerate(files,1)]
            for f in as_completed(fs):results.append(f.result())
        results.sort(key=lambda x:x["i"]);used={e:set() for e in EXTS}
        with zipfile.ZipFile(z.name,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for r in results:
                if not r["ok"]:
                    bad+=1;continue
                temp.append(r["path"]);n=r["name"];base,e=os.path.splitext(n);c=n;k=2
                while c.lower() in used[r["ext"]]:c=f"{base}_{k}{e}";k+=1
                used[r["ext"]].add(c.lower())
                try:
                    with open(r["path"],"rb") as src,archive.open(FOLDERS[r["ext"]]+"/"+c,"w") as dst:
                        while True:
                            chunk=src.read(262144)
                            if not chunk:break
                            dst.write(chunk)
                    ok+=1
                except Exception:bad+=1
        for p in temp:
            try:os.remove(p)
            except Exception:pass
        if not ok:
            try:os.remove(z.name)
            except Exception:pass
            return jsonify({"error":"Aucun fichier n'a pu être téléchargé.","downloaded":0,"failed":bad}),400
        response=send_file(z.name,as_attachment=True,download_name="PDF-Hunter-Documents.zip",mimetype="application/zip")
        response.headers["X-PDF-Hunter-Downloaded"]=str(ok);response.headers["X-PDF-Hunter-Failed"]=str(bad)
        return response
    except Exception as e:
        try:os.remove(z.name)
        except Exception:pass
        return jsonify({"error":f"Impossible de créer le ZIP : {e}"}),500

if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))
