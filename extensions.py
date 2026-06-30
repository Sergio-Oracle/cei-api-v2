"""
Extensions Flask partagées — pattern Factory.

Toutes les extensions sont instanciées ici SANS app, puis initialisées
via extension.init_app(app) dans app.py. Les blueprints importent
depuis ce module au lieu d'importer depuis app.py (évite les imports
circulaires).
"""
from flask_bcrypt import Bcrypt

bcrypt = Bcrypt()
