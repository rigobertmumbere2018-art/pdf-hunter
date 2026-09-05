# PDF Hunter

Petite application web qui analyse une page et détecte les liens directs vers des fichiers PDF publiquement accessibles.

## Installation locale

```bash
pip install -r requirements.txt
python app.py
```

Puis ouvrir :
http://127.0.0.1:5000

## Déploiement

Compatible avec les hébergeurs Python comme Render ou Railway.

Commande de démarrage :
gunicorn app:app

## Limites

L'application détecte les liens PDF directement présents dans le HTML d'une page.
Elle ne contourne pas :
- les paywalls
- les connexions privées
- les protections DRM
- les systèmes d'accès restreint
